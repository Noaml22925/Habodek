"""The review workflow: stages in code, agents for judgement (design §6.6, handoff M6).

    gate -> understand -> finders (6-7 lenses) -> coverage gate -> triage ->
    investigators -> mechanical checks -> verifier A -> verifier B ->
    reverse audit -> judge -> result.json

Under one request budget (handoff §3.1): each stage and agent draws on the review's
`RequestBudget`. Hypotheses are investigated in priority order; what the budget does
not reach is recorded as "not investigated: budget", never silently dropped.

Fail closed: a verifier that errors counts as refuted; an investigation that does not
conclude drops its hypothesis; every drop is kept in `result.findings` with its stage
and reason.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from habodek.brain.codeintel import pydeps
from habodek.brain.codeintel.changemap import SymbolChanges, symbol_changes
from habodek.brain.codeintel.refs import SymbolIndex
from habodek.brain.codeintel.workspace import Workspace
from habodek.brain.contract import (
    Anchor,
    EvidenceLink,
    Finding,
    Result,
    ReviewJob,
    StageStat,
    Verification,
)
from habodek.brain.llm.client import LLMClient
from habodek.brain.memory.context import memory_section
from habodek.brain.memory.recall import recall_tool
from habodek.brain.memory.rules import team_rules
from habodek.brain.memory.store import MemoryView
from habodek.brain.runtime.loop import AgentTask, LoopEngine, RunResult
from habodek.brain.runtime.tool import Tool, ToolContext, code_tools
from habodek.brain.sandbox.core import RunLog
from habodek.brain.sandbox.tool import (
    WITNESSES_SCHEMA,
    check_witnesses,
    run_tool,
    witnesses_from,
)
from habodek.brain.trace.writer import TraceWriter, new_run_id
from habodek.brain.workflow import budget as caps
from habodek.brain.workflow import evidence
from habodek.brain.workflow.budget import RequestBudget
from habodek.brain.workflow.comment import to_comment
from habodek.brain.workflow.prompts import LENSES, Lens, render

SEVERITIES = ["critical", "major", "minor"]
# Roles given the sandbox's `run` tool when one is configured (finders read only).
RUN_ROLES = ("investigator", "verifier_a", "verifier_b")
SKIP_GLOBS = ("vendor/", "third_party/", "node_modules/", ".min.js", "_pb2.py", "generated/")


@dataclass
class ReviewConfig:
    review_cap: int = caps.REVIEW_CAP
    stage_caps: dict[str, int] = field(default_factory=lambda: dict(caps.STAGE_CAPS))
    agent_caps: dict[str, int] = field(default_factory=lambda: dict(caps.AGENT_CAPS))
    max_investigations: int = 10
    parallel: int = 3
    evidence_tokens: int = 80_000
    counterframe: bool = False  # the optional seventh lens
    reverse_audit_rounds: int = 2
    priority: str = "review"
    internal_packages: tuple[str, ...] = ()


# --------------------------------------------------------------------------- schemas

_LINE_REF = {"type": "object", "properties": {
    "path": {"type": "string"},
    "code": {"type": "string", "description": "The exact code of that added or removed "
                                              "line, copied from the diff."}},
    "required": ["path", "code"]}
_LINK = {"type": "object", "properties": {
    "path": {"type": "string", "description": "Path spec, e.g. 'api/x.py', "
                                              "'repo:<name>:path', 'dep:<pkg>@<ver>:path'."},
    "line": {"type": "integer"},
    "quote": {"type": "string", "description": "That line's code, copied exactly."},
    "claim": {"type": "string", "description": "What this line contributes."}},
    "required": ["path", "line", "quote", "claim"]}
_SEVERITY = {"type": "string", "enum": SEVERITIES}

PROPOSE_SCHEMA = {"type": "object", "properties": {
    "receipt": {"type": "string", "minLength": 10},
    "hypotheses": {"type": "array", "items": {"type": "object", "properties": {
        "claim": {"type": "string", "minLength": 10},
        "why": {"type": "string"},
        "where": {"type": "array", "items": {"type": "string"}},
        "refute_by": {"type": "string"},
        "severity": _SEVERITY,
        "changed_line": _LINE_REF},
        "required": ["claim", "why", "severity", "changed_line"]}}},
    "required": ["receipt", "hypotheses"]}

UNDERSTAND_SCHEMA = {"type": "object", "properties": {
    "purpose": {"type": "string", "minLength": 10},
    "contracts": {"type": "array", "items": {"type": "object", "properties": {
        "symbol": {"type": "string"}, "change": {"type": "string"},
        "dependents": {"type": "array", "items": {"type": "string"}}},
        "required": ["symbol", "change"]}},
    "unmentioned": {"type": "array", "items": {"type": "string"}}},
    "required": ["purpose", "contracts"]}

CONCLUDE_SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": ["confirmed", "refuted", "inconclusive"]},
    "reason": {"type": "string", "minLength": 10,
               "description": "Why: for refuted, what prevents the failure (cite it)."},
    "title": {"type": "string"},
    "severity": _SEVERITY,
    "claim": {"type": "string"},
    "scenario": {"type": "string"},
    "fix": {"type": "string"},
    "changed_line": _LINE_REF,
    "chain": {"type": "array", "items": _LINK},
    "witnesses": WITNESSES_SCHEMA},
    "required": ["verdict", "reason"]}

VERDICT_SCHEMA = {"type": "object", "properties": {
    "outcome": {"type": "string", "enum": ["upheld", "refuted", "uncertain"]},
    "reason": {"type": "string", "minLength": 10},
    "evidence": {"type": "array", "items": _LINK},
    "witnesses": WITNESSES_SCHEMA},
    "required": ["outcome", "reason"]}

DECIDE_SCHEMA = {"type": "object", "properties": {
    "post": {"type": "array", "items": {"type": "string"}},
    "drop": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["id", "reason"]}},
    "summary": {"type": "string", "minLength": 10}},
    "required": ["post", "drop", "summary"]}


# --------------------------------------------------------------------------- state


@dataclass
class Hypothesis:
    id: str
    claim: str
    why: str
    severity: str
    changed_path: str
    changed_code: str
    where: list[str] = field(default_factory=list)
    refute_by: str = ""
    lenses: list[str] = field(default_factory=list)

    def render(self) -> str:
        where = ", ".join(self.where) or "-"
        return (f"Hypothesis {self.id} ({self.severity}, from: {', '.join(self.lenses)})\n"
                f"Claim: {self.claim}\nWhy suspected: {self.why}\nWhere to look: {where}\n"
                f"Would be refuted by: {self.refute_by or '-'}\n"
                f"Changed line: {self.changed_path}: `{self.changed_code}`")

    def placeholder(self, status: str, stage: str, reason: str) -> Finding:
        return Finding(id=self.id, title=self.claim[:120], severity=self.severity,
                       claim=self.claim, anchor=Anchor(path=self.changed_path or "?", line=0,
                                                       snippet=self.changed_code),
                       status=status, dropped_at=stage, drop_reason=reason[:500])


class Review:
    def __init__(self, job: ReviewJob, ws: Workspace, client: LLMClient, trace: TraceWriter,
                 config: ReviewConfig | None = None, index: SymbolIndex | None = None,
                 runs: RunLog | None = None, memory: MemoryView | None = None) -> None:
        self.job, self.ws, self.client, self.trace = job, ws, client, trace
        self.runs = runs
        self.memory = memory
        self.rules = ""  # team rules: for the agents that propose, never those that refute
        self.config = config or ReviewConfig()
        if job.request_budget:
            self.config.review_cap = job.request_budget
        self.index = index or SymbolIndex(ws)
        self.engine = LoopEngine(client, trace)
        self.budget = RequestBudget(self.config.review_cap, "review")
        self.run_id = trace.run_id if trace.run_id != "untraced" else new_run_id()
        self.stats: list[StageStat] = []
        self.findings: list[Finding] = []  # every outcome: posted, dropped, not investigated
        self.not_investigated: list[str] = []
        self.unreviewed_lenses: list[str] = []
        self.receipts: dict[str, str] = {}
        self.investigated = 0
        self.changes: SymbolChanges | None = None
        self.bumps: list[pydeps.DependencySource] = []
        self.pack = ""
        self.understanding = ""
        self._seq = 0

    # ------------------------------------------------------------------ helpers
    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def _model(self, role: str) -> str:
        override = self.job.models.roles.get(role)
        return override or self.client.profiles.roles.get(role) or role

    def _stage(self, name: str) -> RequestBudget:
        return self.budget.child(name, self.config.stage_caps.get(name, 20))

    def _ctx(self, label: str = "") -> ToolContext:
        return ToolContext(ws=self.ws, index=self.index, extra={"agent": label})

    def _agent(self, role: str, label: str, system: str, task: str, terminal: Tool,
               stage_budget: RequestBudget, cap_key: str | None = None) -> RunResult:
        cap = self.config.agent_caps.get(cap_key or role, 10)
        tools = code_tools(include_git=self.ws.repo is not None)
        if self.memory is not None:
            tools.append(recall_tool(self.memory))
        if self.runs is not None and role in RUN_ROLES:
            tools.append(run_tool(self.runs))
        agent = AgentTask(role=role, label=label, system=system, task=task,
                          tools=[*tools, terminal],
                          model=self._model(role), max_turns=cap + 4, max_requests=cap,
                          priority=self.config.priority)
        return self.engine.run(agent, self._ctx(label), stage_budget)

    def _record_stage(self, name: str, stage: RequestBudget, agents: int, started: float,
                      notes: str = "") -> None:
        stat = StageStat(stage=name, requests=stage.used, agents=agents,
                         seconds=round(time.monotonic() - started, 1), notes=notes)
        self.stats.append(stat)
        self.trace.event("stage_end", **stat.model_dump())

    # ------------------------------------------------------------------ gate
    def gate(self) -> str | None:
        if self.job.mr.draft:
            return "draft merge request"
        files = self.ws.change_map().files
        if not files:
            return "empty diff"
        if all(any(g in f.path for g in SKIP_GLOBS) for f in files):
            return "only vendored or generated files changed"
        return None

    # ------------------------------------------------------------------ prepare
    def prepare(self) -> None:
        self.changes = symbol_changes(self.ws, self.index.cache)
        self.bumps = bumps_from_job(self.job)
        diff_text = self.ws.git_diff(self.ws.base_sha, self.ws.head_sha) if self.ws.repo else ""
        pack = evidence.build_pack(self.ws, self.index, self.changes, title=self.job.mr.title,
                                   description=self.job.mr.description, diff_text=diff_text,
                                   bumps=self.bumps)
        changed = self.ws.changed_paths() if self.ws.repo else []
        pack.add("Repository memory", memory_section(self.memory, changed), 1)
        rules = team_rules(self.ws.base, changed)
        self.rules = rules.render()
        self.trace.event("team_rules", files=rules.files, dropped=rules.dropped)
        self.pack = pack.render(self.config.evidence_tokens)
        self.trace.event("evidence_pack", chars=len(self.pack), dropped=pack.dropped)
        trace_dir = getattr(self.trace, "dir", None)
        if trace_dir is not None:
            (trace_dir / "evidence_pack.md").write_text(self.pack, encoding="utf-8")

    # ------------------------------------------------------------------ understand
    def understand(self) -> None:
        started, stage = time.monotonic(), self._stage("understand")
        terminal = Tool("record_understanding", "Record what the MR is for and which "
                        "contracts it changes.", UNDERSTAND_SCHEMA, terminal=True)
        run = self._agent("understand", "understand",
                          render("understand", terminal=terminal.name),
                          (f"{self.rules}\n\n" if self.rules else "")
                          + f"# Evidence\n\n{self.pack}", terminal, stage)
        if run.ok:
            out = run.output or {}
            contracts = "\n".join(
                f"- {c.get('symbol')}: {c.get('change')}"
                + (f" (dependents: {', '.join(c.get('dependents') or [])})"
                   if c.get("dependents") else "") for c in out.get("contracts") or [])
            unmentioned = "\n".join(f"- {u}" for u in out.get("unmentioned") or [])
            self.understanding = (f"Purpose: {out.get('purpose', '')}\n\nContracts changed:\n"
                                  f"{contracts or '- (none recorded)'}")
            if unmentioned:
                self.understanding += f"\n\nNot mentioned in the description:\n{unmentioned}"
        self._record_stage("understand", stage, 1, started, run.status)

    # ------------------------------------------------------------------ hypothesise
    def _finder_task(self, extra: str = "") -> str:
        parts = []
        if self.understanding:
            parts.append(f"# What the change is (from the lead reviewer)\n\n{self.understanding}")
        if self.rules:
            parts.append(self.rules)
        if extra:
            parts.append(extra)
        parts.append(f"# Evidence\n\n{self.pack}")
        return "\n\n".join(parts)

    def _propose_tool(self) -> Tool:
        return Tool("propose_hypotheses", "Return what you walked and the hypotheses worth "
                    "investigating.", PROPOSE_SCHEMA, terminal=True)

    def _run_finder(self, lens: Lens, stage: RequestBudget, label: str,
                    extra: str = "") -> RunResult:
        tool = self._propose_tool()
        return self._agent("finder", label, render("finder", lens=lens, terminal=tool.name),
                           self._finder_task(extra), tool, stage)

    @staticmethod
    def _covered(run: RunResult) -> bool:
        """Coverage from the transcript, not self-report: it concluded, it read code,
        and it gave a receipt of what it walked."""
        receipt = str((run.output or {}).get("receipt", ""))
        return run.ok and len(run.calls) >= 1 and len(receipt.strip()) >= 20

    def _collect(self, run: RunResult, lens_name: str) -> list[Hypothesis]:
        out: list[Hypothesis] = []
        for raw in (run.output or {}).get("hypotheses") or []:
            changed = raw.get("changed_line") or {}
            out.append(Hypothesis(
                id=self._id("H"), claim=str(raw.get("claim", "")).strip(),
                why=str(raw.get("why", "")).strip(),
                severity=raw.get("severity") if raw.get("severity") in SEVERITIES else "major",
                changed_path=str(changed.get("path", "")).strip(),
                changed_code=str(changed.get("code", "")).strip(),
                where=[str(w) for w in raw.get("where") or []][:10],
                refute_by=str(raw.get("refute_by", "")).strip(), lenses=[lens_name]))
        return out

    def hypothesise(self) -> list[Hypothesis]:
        started, stage = time.monotonic(), self._stage("hypothesise")
        lenses = [lens for lens in LENSES if lens.name != "counterframe" or
                  self.config.counterframe]
        with ThreadPoolExecutor(max_workers=self.config.parallel) as pool:
            runs = list(pool.map(lambda lens: (lens, self._run_finder(
                lens, stage, f"finder/{lens.name}")), lenses))
        hypotheses: list[Hypothesis] = []
        agents = len(runs)
        for lens, run in runs:
            if not self._covered(run) and stage.remaining >= self.config.agent_caps["finder"]:
                self.trace.event("coverage_relaunch", lens=lens.name, status=run.status,
                                 calls=len(run.calls))
                run = self._run_finder(lens, stage, f"finder/{lens.name}/retry",
                                       "# Note\n\nAn earlier pass of this lens returned no "
                                       "receipt of a walk. Walk the code with the tools first.")
                agents += 1
            if self._covered(run):
                self.receipts[lens.name] = str(run.output.get("receipt", "")).strip()
                hypotheses += self._collect(run, lens.name)
            else:
                self.unreviewed_lenses.append(lens.name)
                if run.ok:  # concluded without walking: keep any suspected critical
                    hypotheses += [h for h in self._collect(run, lens.name)
                                   if h.severity == "critical"]
        self._record_stage("hypothesise", stage, agents, started,
                           f"{len(hypotheses)} hypotheses")
        return hypotheses

    # ------------------------------------------------------------------ triage
    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z_][a-z0-9_]{2,}", text.lower())}

    def triage(self, hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        merged: list[Hypothesis] = []
        for h in hypotheses:
            if not h.claim:
                continue
            twin = None
            for m in merged:
                same_line = (evidence._norm(m.changed_code) == evidence._norm(h.changed_code)
                             and m.changed_path == h.changed_path)
                a, b = self._tokens(m.claim), self._tokens(h.claim)
                similar = bool(a and b) and len(a & b) / len(a | b) >= 0.5
                if same_line and similar:
                    twin = m
                    break
            if twin is None:
                merged.append(h)
                continue
            twin.lenses += [lens for lens in h.lenses if lens not in twin.lenses]
            twin.where += [w for w in h.where if w not in twin.where]
            if SEVERITIES.index(h.severity) < SEVERITIES.index(twin.severity):
                twin.severity = h.severity
        merged.sort(key=lambda h: (SEVERITIES.index(h.severity), -len(h.lenses)))
        self.trace.event("triage", kept=[h.id for h in merged],
                         merged=len(hypotheses) - len(merged))
        return merged

    # ------------------------------------------------------------------ investigate
    def _conclude_tool(self) -> Tool:
        def check(args: dict[str, Any], ctx: ToolContext) -> str | None:
            bad_runs = check_witnesses(self.runs, args.get("witnesses"))
            if bad_runs:
                return bad_runs
            if args.get("verdict") != "confirmed":
                return None
            missing = [k for k in ("title", "claim", "scenario", "changed_line", "chain")
                       if not args.get(k)]
            if missing:
                return f"a confirmed finding needs {', '.join(missing)}."
            if len(args["chain"]) < 2:
                return ("the evidence chain needs at least two links: the changed line and "
                        "the site where it fails.")
            bad = []
            for i, link in enumerate(args["chain"], 1):
                check_result = evidence.check_quote(self.ws, link)
                if not check_result.ok:
                    bad.append(f"link {i}: {check_result.reason}")
            if bad:
                return ("these quotes are not in the code: " + "; ".join(bad)
                        + ". Copy each quote from a tool result.")
            changed = args["changed_line"]
            if evidence.anchor_from_snippet(self.ws, changed.get("path", ""),
                                            changed.get("code", "")) is None:
                return (f"changed_line is not an added or removed line of the diff of "
                        f"{changed.get('path')!r}. Name the changed line that causes this.")
            return None

        return Tool("conclude", "Record the outcome of the investigation.", CONCLUDE_SCHEMA,
                    terminal=True, check=check)

    def _investigate_one(self, h: Hypothesis, stage: RequestBudget) -> tuple[Hypothesis,
                                                                             RunResult]:
        tool = self._conclude_tool()
        task = f"# The hypothesis\n\n{h.render()}\n\n# Evidence\n\n{self.pack}"
        return h, self._agent("investigator", f"investigate/{h.id}",
                              render("investigator", terminal=tool.name,
                                     sandbox=self.runs is not None), task, tool, stage)

    def investigate(self, hypotheses: list[Hypothesis], stage: RequestBudget) -> list[Finding]:
        cap = self.config.agent_caps["investigator"]
        chosen: list[Hypothesis] = []
        for h in hypotheses:
            affordable = stage.remaining - len(chosen) * cap >= min(cap, 6)
            if len(chosen) < self.config.max_investigations and affordable:
                chosen.append(h)
            else:
                self.not_investigated.append(h.claim[:160])
                self.findings.append(h.placeholder("not_investigated", "investigate",
                                                   "not investigated: budget"))
        self.investigated += len(chosen)
        with ThreadPoolExecutor(max_workers=self.config.parallel) as pool:
            done = list(pool.map(lambda h: self._investigate_one(h, stage), chosen))
        confirmed: list[Finding] = []
        for h, run in done:
            out = run.output or {}
            if not run.ok:
                self.findings.append(h.placeholder("dropped", "investigate",
                                                   f"{run.status}: {run.error}"))
                continue
            if out.get("verdict") != "confirmed":
                self.findings.append(h.placeholder("dropped", "investigate",
                                                   f"{out.get('verdict')}: {out.get('reason')}"))
                continue
            changed = out.get("changed_line") or {}
            finding = Finding(
                id=h.id, title=str(out.get("title") or h.claim)[:160],
                severity=out.get("severity") if out.get("severity") in SEVERITIES
                else h.severity, category=",".join(h.lenses), claim=str(out.get("claim")),
                scenario=str(out.get("scenario") or ""), fix=str(out.get("fix") or ""),
                anchor=Anchor(path=str(changed.get("path", "")), line=0,
                              snippet=str(changed.get("code", ""))),
                chain=[EvidenceLink(path=str(link.get("path", "")),
                                    line=int(link.get("line") or 0),
                                    quote=str(link.get("quote", "")),
                                    claim=str(link.get("claim", "")))
                       for link in out.get("chain") or []],
                witnesses=witnesses_from(self.runs, out.get("witnesses")),
                head_sha=self.job.revisions.head_sha)
            confirmed.append(finding)
        return confirmed

    # ------------------------------------------------------------------ checks
    def check(self, candidates: list[Finding]) -> list[Finding]:
        passed: list[Finding] = []
        for f in candidates:
            anchor = evidence.anchor_from_snippet(self.ws, f.anchor.path, f.anchor.snippet)
            if anchor is None:
                for link in f.chain:
                    anchor = evidence.anchor_from_snippet(self.ws, link.path, link.quote)
                    if anchor is not None:
                        break
            if anchor is None:
                self._drop(f, "check:anchor", "does not name a changed line of the diff")
                continue
            links: list[EvidenceLink] = []
            rejected: list[str] = []
            for link in f.chain:
                result = evidence.check_quote(self.ws, link)
                if result.ok and result.link is not None:
                    if not any((x.repo, x.path, x.line) == (result.link.repo, result.link.path,
                                                            result.link.line) for x in links):
                        links.append(result.link)
                else:
                    rejected.append(result.reason)
            anchor_link = EvidenceLink(path=anchor.path, line=anchor.line, quote=anchor.snippet,
                                       claim="the changed line",
                                       revision="head" if anchor.side == "new" else "base")
            if not any(x.repo is None and x.path == anchor.path and x.line == anchor.line
                       for x in links):
                links.insert(0, anchor_link)
            if rejected:
                self.trace.event("check_quote_rejected", finding=f.id, reasons=rejected)
            if len(links) < 2:
                self._drop(f, "check:evidence", "no verified evidence beyond the changed line"
                           + (f" ({'; '.join(rejected)})" if rejected else ""))
                continue
            f.anchor, f.chain = anchor, links
            # Stable across re-reviews of the same MR: the marker on GitLab keys on it.
            f.id = finding_id(anchor.path, anchor.snippet, f.title)
            passed.append(f)
        # One finding per root cause: the same anchored line.
        by_anchor: dict[tuple[str, int, str], Finding] = {}
        for f in passed:
            key = (f.anchor.path, f.anchor.line, f.anchor.side)
            keep = by_anchor.get(key)
            if keep is None:
                by_anchor[key] = f
                continue
            if SEVERITIES.index(f.severity) < SEVERITIES.index(keep.severity):
                keep, f = f, keep
                by_anchor[key] = keep
            for link in f.chain:
                if not any((x.repo, x.path, x.line) == (link.repo, link.path, link.line)
                           for x in keep.chain):
                    keep.chain.append(link)
            keep.witnesses += [w for w in f.witnesses
                               if all(w.id != x.id for x in keep.witnesses)]
            self._drop(f, "check:merge", f"same root cause as {keep.id}")
        return list(by_anchor.values())

    def _drop(self, f: Finding, stage: str, reason: str) -> None:
        f.status, f.dropped_at, f.drop_reason = "dropped", stage, reason[:500]
        self.findings.append(f)
        self.trace.event("finding_dropped", finding=f.id, stage=stage, reason=reason[:500])

    # ------------------------------------------------------------------ verify
    def _verify_tool(self) -> Tool:
        def check(args: dict[str, Any], ctx: ToolContext) -> str | None:
            bad_runs = check_witnesses(self.runs, args.get("witnesses"))
            if bad_runs:
                return bad_runs
            if args.get("outcome") == "refuted" and not (args.get("evidence")
                                                         or args.get("witnesses")):
                return ("a refutation must cite the code that refutes it (evidence) or a run "
                        "that shows it (witnesses).")
            return None

        return Tool("verdict", "Record whether the finding survives.", VERDICT_SCHEMA,
                    terminal=True, check=check)

    def _render_finding(self, f: Finding) -> str:
        chain = "\n".join(f"{i}. {(link.repo + ':') if link.repo else ''}{link.path}:"
                          f"{link.line} `{link.quote}` — {link.claim}"
                          for i, link in enumerate(f.chain, 1))
        runs = "".join(f"\n- {w.id} ({w.root}) `{w.cmd}` exited {w.exit_code}: {w.shows}\n"
                       f"```\n{w.output_tail[-800:]}\n```" for w in f.witnesses)
        if runs:
            chain += "\n\nSandbox runs cited by the investigator (re-run them to check):" + runs
        return (f"Finding {f.id} ({f.severity}): {f.title}\n\nClaim: {f.claim}\n\n"
                f"Failing scenario: {f.scenario}\n\nAnchored at the changed line "
                f"{f.anchor.path}:{f.anchor.line} `{f.anchor.snippet}`\n\nEvidence chain:\n"
                f"{chain}\n\nProposed fix: {f.fix or '-'}")

    def _verify_one(self, f: Finding, role: str, stage: RequestBudget) -> Verification:
        tool = self._verify_tool()
        task = f"# The finding to check\n\n{self._render_finding(f)}\n\n# Evidence\n\n{self.pack}"
        run = self._agent(role, f"{role}/{f.id}",
                          render("verifier", terminal=tool.name, sandbox=self.runs is not None),
                          task, tool, stage)
        if not run.ok:
            return Verification(verifier=role, model=run.model, outcome="error",
                                reason=f"{run.status}: {run.error}"[:500])
        out = run.output or {}
        links = []
        for raw in out.get("evidence") or []:
            checked = evidence.check_quote(self.ws, raw)
            if checked.ok and checked.link is not None:
                links.append(checked.link)
        return Verification(verifier=role, model=run.model, outcome=out.get("outcome"),
                            reason=str(out.get("reason", ""))[:1000], evidence=links,
                            witnesses=witnesses_from(self.runs, out.get("witnesses")))

    def verify(self, findings: list[Finding]) -> list[Finding]:
        upheld: list[Finding] = []
        for role, stage_name in (("verifier_a", "verify_a"), ("verifier_b", "verify_b")):
            if not findings:
                break
            started, stage = time.monotonic(), self._stage(stage_name)
            with ThreadPoolExecutor(max_workers=self.config.parallel) as pool:
                results = list(pool.map(lambda f, r=role, s=stage: (f, self._verify_one(f, r, s)),
                                        findings))
            survivors = []
            for f, v in results:
                f.verifications.append(v)
                if v.outcome == "upheld":
                    survivors.append(f)
                else:  # refuted, uncertain or error: fail closed
                    self._drop(f, stage_name, f"{v.outcome}: {v.reason}")
            self._record_stage(stage_name, stage, len(results), started,
                               f"{len(survivors)}/{len(results)} upheld")
            findings = survivors
        upheld = findings
        return upheld

    # ------------------------------------------------------------------ reverse audit
    def reverse_audit(self, upheld: list[Finding], all_hypotheses: list[Hypothesis]
                      ) -> list[Finding]:
        found: list[Finding] = []
        for round_no in range(1, self.config.reverse_audit_rounds + 1):
            started, stage = time.monotonic(), self._stage("reverse_audit")
            listed = "\n".join(f"- {f.title} ({f.anchor.path}:{f.anchor.line})"
                               for f in upheld + found) or "- (no findings)"
            refuted = "\n".join(f"- {h.claim}" for h in all_hypotheses) or "- (none)"
            extra = (f"# Reverse audit, round {round_no}\n\nOther reviewers already reported:\n"
                     f"{listed}\n\nand investigated these hypotheses:\n{refuted}\n\nWhat is "
                     "*not* on these lists? Propose only new hypotheses.")
            lens = Lens("reverse", "what everyone else missed",
                        ("Re-read the diff and the changed functions with fresh eyes.",
                         "Look where the other lists are thin: other callers, other paths, "
                         "removed lines, tests that changed.",))
            run = self._run_finder(lens, stage, f"reverse_audit/{round_no}", extra)
            self._record_stage("reverse_audit", stage, 1, started, run.status)
            new = self.triage(self._collect(run, "reverse")) if run.ok else []
            known = {evidence._norm(h.claim) for h in all_hypotheses}
            new = [h for h in new if evidence._norm(h.claim) not in known]
            if not new:
                break
            all_hypotheses += new
            inv_stage = self._stage("investigate")
            confirmed = self.check(self.investigate(new, inv_stage))
            round_found = self.verify(confirmed)
            found += round_found
            if not round_found:
                break
        return found

    # ------------------------------------------------------------------ judge
    def judge(self, upheld: list[Finding]) -> tuple[list[Finding], str]:
        ordered = sorted(upheld, key=lambda f: SEVERITIES.index(f.severity))
        if not upheld:
            return [], ""
        started, stage = time.monotonic(), self._stage("judge")
        tool = Tool("decide", "Choose the comments to post and write the summary.",
                    DECIDE_SCHEMA, terminal=True,
                    check=lambda args, ctx: self._check_decision(args, upheld))
        listing = "\n\n---\n\n".join(self._render_finding(f) for f in ordered)
        task = (f"# The merge request\n\nTitle: {self.job.mr.title}\n\n"
                f"{self.job.mr.description}\n\n# Verified findings\n\n{listing}\n\n"
                f"# What was checked\n\n{self._checked_text()}")
        run = self._agent("judge", "judge",
                          render("judge", terminal=tool.name,
                                 max_comments=self.job.max_comments), task, tool, stage)
        self._record_stage("judge", stage, 1, started, run.status)
        if not run.ok:  # deterministic fallback: they all survived two verifiers
            return ordered[: self.job.max_comments], ""
        out = run.output or {}
        by_id = {f.id: f for f in upheld}
        post = [by_id[i] for i in out.get("post") or [] if i in by_id][: self.job.max_comments]
        for item in out.get("drop") or []:
            f = by_id.get(item.get("id"))
            if f is not None and f not in post:
                self._drop(f, "judge", str(item.get("reason", "")))
        for f in upheld:  # anything neither posted nor dropped explicitly
            if f not in post and f.status != "dropped":
                self._drop(f, "judge", "not selected")
        return post, str(out.get("summary", "")).strip()

    @staticmethod
    def _check_decision(args: dict[str, Any], upheld: list[Finding]) -> str | None:
        ids = {f.id for f in upheld}
        named = set(args.get("post") or []) | {d.get("id") for d in args.get("drop") or []}
        unknown = named - ids
        if unknown:
            return f"unknown finding ids: {', '.join(sorted(unknown))}; use {sorted(ids)}."
        return None

    # ------------------------------------------------------------------ summary
    def _checked_text(self) -> str:
        lines = [f"- {lens}: {receipt}" for lens, receipt in self.receipts.items()]
        return "\n".join(lines) or "- (no receipts)"

    def summary(self, posted: list[Finding], judge_text: str) -> str:
        dropped = [f for f in self.findings if f.status == "dropped"]
        parts = []
        if judge_text:
            parts.append(judge_text)
        elif posted:
            parts.append(f"{len(posted)} issue(s) found; see the inline comments.")
        else:
            parts.append("No issues found.")
        refuted = [f for f in dropped if f.dropped_at in ("investigate", "verify_a",
                                                          "verify_b")]
        parts.append(f"Hypotheses investigated: {self.investigated}; refuted or unconfirmed: "
                     f"{len(refuted)}.")
        if self.receipts:
            parts.append("Checked:\n" + self._checked_text())
        if self.not_investigated:
            parts.append(f"Not investigated (request budget): {len(self.not_investigated)} — "
                         + "; ".join(self.not_investigated[:3]))
        if self.unreviewed_lenses:
            parts.append("Not reviewed (no walk recorded): " + ", ".join(self.unreviewed_lenses))
        if self._is_library():
            consumers = sorted(self.job.workspace.other_repos)
            parts.append("This is a library. Consumers checked: " + ", ".join(consumers) + "."
                         if consumers else "This is a library: no consumers in other "
                         "repositories are known yet (they appear once their projects are "
                         "mapped), so none were checked.")
        return "\n\n".join(parts)

    def _is_library(self) -> bool:
        pyproject = self.ws.head / "pyproject.toml"
        if not pyproject.is_file() or not self.config.internal_packages:
            return False
        match = re.search(r'^name\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
        return bool(match and pydeps.is_internal(match.group(1), self.config.internal_packages))

    # ------------------------------------------------------------------ run
    def run(self) -> Result:
        started = time.monotonic()
        self.trace.event("review_start", job=self.job.job_id, mr=self.job.mr.model_dump(),
                         revisions=self.job.revisions.model_dump(), cap=self.config.review_cap)
        reason = self.gate()
        if reason:
            return self._result("skipped", [], f"Skipped: {reason}.", reason)
        self.prepare()
        self.understand()
        hypotheses = self.triage(self.hypothesise())
        inv_started, inv_stage = time.monotonic(), self._stage("investigate")
        confirmed = self.investigate(hypotheses, inv_stage)
        self._record_stage("investigate", inv_stage, len(hypotheses), inv_started,
                           f"{len(confirmed)} confirmed")
        checked = self.check(confirmed)
        upheld = self.verify(checked)
        upheld += self.reverse_audit(upheld, hypotheses)
        posted, judge_text = self.judge(upheld)
        result = self._result("ok", posted, self.summary(posted, judge_text))
        self.trace.event("review_end", seconds=round(time.monotonic() - started, 1),
                         posted=len(posted), requests=self.budget.used)
        return result

    def _result(self, status: str, posted: list[Finding], summary: str,
                reason: str = "") -> Result:
        checked = f"Checked {sum(1 for _ in self.receipts)} lenses"
        comments = [to_comment(f, self.job.mr, self.job.revisions.head_sha, self.run_id, checked)
                    for f in posted]
        for f in posted:
            f.status = "posted"
        result = Result(job_id=self.job.job_id, run_id=self.run_id, kind="review",
                        status=status, reason=reason, comments=comments, summary=summary,
                        findings=[*posted, *self.findings],
                        not_investigated=self.not_investigated, stats=self.stats,
                        requests=self.budget.used, llm=self.client.stats.as_dict(),
                        memory_commit=self.memory.commit if self.memory else "")
        self.trace.write_json("findings.json", [f.model_dump() for f in result.findings])
        return result


def bumps_from_job(job: ReviewJob) -> list[pydeps.DependencySource]:
    """Dependency sources the control plane prepared, grouped into base/head pairs."""
    by_name: dict[str, dict[str, Any]] = {}
    for tree in job.workspace.dependencies:
        entry = by_name.setdefault(tree.name, {})
        if tree.side in ("both", "head"):
            entry["head"] = tree
        if tree.side in ("both", "base"):
            entry["base"] = tree
    out = []
    for name, entry in by_name.items():
        base, head = entry.get("base"), entry.get("head")
        out.append(pydeps.DependencySource(
            name, base.version if base else None, head.version if head else None,
            Path(base.path) if base else None, Path(head.path) if head else None))
    return out


def run_review(job: ReviewJob, ws: Workspace, client: LLMClient, trace: TraceWriter,
               config: ReviewConfig | None = None, runs: RunLog | None = None,
               memory: MemoryView | None = None) -> Result:
    review = Review(job, ws, client, trace, config, runs=runs, memory=memory)
    try:
        return review.run()
    except Exception as exc:  # noqa: BLE001 - the control plane needs a result.json
        trace.event("review_failed", error=f"{type(exc).__name__}: {exc}")
        return Result(job_id=job.job_id, run_id=review.run_id, kind="review", status="failed",
                      reason=f"{type(exc).__name__}: {str(exc)[:500]}",
                      findings=review.findings, stats=review.stats,
                      requests=review.budget.used, llm=client.stats.as_dict())


def finding_id(path: str, snippet: str, title: str) -> str:
    key = f"{path}:{evidence._norm(snippet)}:{evidence._norm(title).lower()}"
    return "f" + hashlib.sha1(key.encode()).hexdigest()[:10]


def dumps(result: Result) -> str:
    return json.dumps(result.model_dump(), indent=2, ensure_ascii=False)
