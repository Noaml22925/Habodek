"""The cartographer: writes a project's repo card from its default branch (M10).

    habodek-brain map --job job.json --out result.json --trace DIR

Facts that code can establish are established by code (package, pins, layout, test
recipe, hot spots from history). Judgement goes to *surveyor* agents, one per area of
the repository (a top-level package or directory), in parallel, at background
priority, under one request budget (300). Every entry point and invariant a surveyor
records must quote the code that establishes it; the quote is checked against the
tree, and what does not check out is dropped — the card is in every future
reviewer's context, so it fails closed like a finding.

The result carries two memory updates, `card` (markdown, with a Team notes section
the merge preserves) and `facts` (JSON: the reverse-dependency index is built from
every project's facts).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from habodek.brain.codeintel import pydeps
from habodek.brain.codeintel.refs import SymbolIndex
from habodek.brain.codeintel.workspace import Workspace
from habodek.brain.contract import MapJob, MemoryUpdate, Result, StageStat
from habodek.brain.llm.client import LLMClient
from habodek.brain.memory.store import TEAM_NOTES_END, TEAM_NOTES_START, MemoryView
from habodek.brain.runtime.loop import AgentTask, LoopEngine
from habodek.brain.runtime.tool import Tool, ToolContext, code_tools
from habodek.brain.sandbox.core import recipe_for
from habodek.brain.trace.writer import TraceWriter, new_run_id
from habodek.brain.workflow import evidence
from habodek.brain.workflow.budget import CARTOGRAPHER_CAP, RequestBudget
from habodek.brain.workflow.prompts import render

MAX_AREAS = 8
CARD_CHARS = 9000
SKIP = {"tests", "test", "docs", "doc", "examples", "scripts", "vendor", "third_party",
        "node_modules", "build", "dist", "migrations"}
_LINK = {"type": "object", "properties": {
    "path": {"type": "string"}, "line": {"type": "integer"},
    "quote": {"type": "string", "description": "That line's code, copied exactly."}},
    "required": ["path", "line", "quote"]}
SURVEY_SCHEMA = {"type": "object", "properties": {
    "receipt": {"type": "string", "minLength": 10,
                "description": "What you read (files, symbols traced)."},
    "summary": {"type": "string", "minLength": 10},
    "modules": {"type": "array", "items": {"type": "object", "properties": {
        "path": {"type": "string"}, "responsibility": {"type": "string"}},
        "required": ["path", "responsibility"]}},
    "entry_points": {"type": "array", "items": {"type": "object", "properties": {
        "what": {"type": "string"}, "at": _LINK}, "required": ["what", "at"]}},
    "invariants": {"type": "array", "items": {"type": "object", "properties": {
        "statement": {"type": "string", "minLength": 10},
        "evidence": {"type": "array", "items": _LINK, "minItems": 1}},
        "required": ["statement", "evidence"]}}},
    "required": ["receipt", "summary", "modules", "invariants"]}


@dataclass
class Survey:
    area: str
    ok: bool = False
    summary: str = ""
    modules: list[dict] = field(default_factory=list)
    entry_points: list[tuple[str, Any]] = field(default_factory=list)  # (what, EvidenceLink)
    invariants: list[tuple[str, list[Any]]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    error: str = ""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def areas(head: Path) -> list[str]:
    """Top-level packages/directories with Python code, biggest first; '.' for root files."""
    sizes: Counter[str] = Counter()
    for path in head.rglob("*.py"):
        rel = path.relative_to(head)
        if any(p.startswith(".") or p in SKIP for p in rel.parts[:-1]):
            continue
        sizes[rel.parts[0] if len(rel.parts) > 1 else "."] += 1
    ordered = [a for a, _ in sizes.most_common()]
    if len(ordered) > MAX_AREAS:  # the long tail goes to the last surveyor
        ordered = ordered[:MAX_AREAS - 1] + [",".join(ordered[MAX_AREAS - 1:])]
    return ordered


def hot_spots(repo: Path | None, rev: str, n_commits: int = 300) -> tuple[list, list]:
    if repo is None:
        return [], []
    out = _git(repo, "log", "-n", str(n_commits), "--name-only", "--format=%x00%h %s", rev)
    churn: Counter[str] = Counter()
    fixes: list[str] = []
    for block in out.split("\x00"):
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        subject, files = lines[0], lines[1:]
        churn.update(f for f in files if f.endswith(".py"))
        if re.search(r"\b(fix|bug|regression|revert)", subject, re.IGNORECASE) and files:
            fixes.append(f"`{subject[:90]}` ({', '.join(files[:3])})")
    return churn.most_common(8), fixes[:6]


def _description(head: Path, project: dict) -> str:
    if project.get("description"):
        return str(project["description"])
    readme = next((p for p in (head / "README.md", head / "README.rst", head / "README")
                   if p.is_file()), None)
    if readme is None:
        return ""
    paras = [p.strip() for p in readme.read_text(errors="replace").split("\n\n")]
    return next((p for p in paras if p and not p.startswith("#")), "")[:600]


def collect_facts(job: MapJob, ws: Workspace, internal: tuple[str, ...]) -> dict[str, Any]:
    head = ws.head
    data: dict[str, Any] = {}
    pyproject = head / "pyproject.toml"
    if pyproject.is_file():
        import tomllib

        try:
            data = tomllib.loads(pyproject.read_text()).get("project") or {}
        except ValueError:
            data = {}
    pins = (pydeps.pins_at(ws.repo, ws.head_sha) if ws.repo and ws.head_sha
            else pydeps.pins_in_dir(head))
    churn, fixes = hot_spots(ws.repo, ws.head_sha or "HEAD")
    recipe = recipe_for(head, None)
    return {
        "project": job.mr.project, "gitlab_id": job.mr.project_id,
        "package": pydeps.normalize(data["name"]) if data.get("name") else "",
        "version": str(data.get("version", "")),
        "description": _description(head, data), "default_branch": job.mr.target_branch,
        "sha": ws.head_sha, "mapped_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pins": {name: pin.version for name, pin in sorted(pins.items())},
        "internal_pins": sorted(n for n in pins if internal and pydeps.is_internal(n, internal)),
        "test": (recipe.get("test") or [""])[0], "areas": areas(head),
        "churn": churn, "fixes": fixes,
    }


class Cartographer:
    def __init__(self, job: MapJob, ws: Workspace, client: LLMClient, trace: TraceWriter,
                 memory: MemoryView | None = None, internal: tuple[str, ...] = (),
                 parallel: int = 3, index: SymbolIndex | None = None) -> None:
        self.job, self.ws, self.client, self.trace = job, ws, client, trace
        self.memory, self.internal, self.parallel = memory, internal, parallel
        self.index = index or SymbolIndex(ws)
        self.engine = LoopEngine(client, trace)
        self.budget = RequestBudget(job.request_budget or CARTOGRAPHER_CAP, "map")
        self.run_id = trace.run_id if trace.run_id != "untraced" else new_run_id("m")
        self.stats: list[StageStat] = []

    def _model(self) -> str:
        return (self.job.models.roles.get("cartographer")
                or self.client.profiles.roles.get("cartographer") or "cartographer")

    def _check(self, raw: dict) -> Any | None:
        result = evidence.check_quote(self.ws, {**raw, "claim": raw.get("claim", "")})
        return result.link if result.ok else None

    def _terminal(self) -> Tool:
        def check(args: dict[str, Any], ctx: ToolContext) -> str | None:
            bad = []
            for inv in args.get("invariants") or []:
                for link in inv.get("evidence") or []:
                    if self._check(link) is None:
                        bad.append(f"{link.get('path')}:{link.get('line')}")
            for ep in args.get("entry_points") or []:
                if self._check(ep.get("at") or {}) is None:
                    bad.append(f"{(ep.get('at') or {}).get('path')}")
            if bad and not ctx.extra.get("warned"):
                # Once: a model that insists has its unverified items dropped, not its area.
                ctx.extra["warned"] = True
                return ("these quotes are not in the code: " + ", ".join(bad[:8])
                        + ". Copy each quote from a tool result, or leave the item out "
                        "(items left unverified are dropped).")
            return None

        return Tool("record_survey", "Record your map of this area.", SURVEY_SCHEMA,
                    terminal=True, check=check)

    def _task(self, area: str, facts: dict[str, Any], previous: str) -> str:
        names = ", ".join(f"`{a}`" for a in area.split(",")) if area != "." else \
            "the repository's top-level files"
        files = []
        for a in area.split(","):
            root = self.ws.head if a == "." else self.ws.head / a
            pattern = "*.py" if a == "." else "**/*.py"
            files += [str(p.relative_to(self.ws.head)) for p in sorted(root.glob(pattern))][:60]
        parts = [f"# Your area: {names}", f"Repository: {facts['project']} at "
                 f"{(facts['sha'] or '')[:10]} (default branch {facts['default_branch']}).",
                 f"{facts['description']}".strip(),
                 "Python files in the area:\n" + "\n".join(f"- {f}" for f in files[:80])]
        if facts.get("pins"):
            parts.append("Pinned dependencies: " + ", ".join(
                f"{k}=={v}" for k, v in facts["pins"].items()))
        if previous:
            parts.append(f"# The earlier card\n\n{previous[:6000]}")
        return "\n\n".join(p for p in parts if p)

    def survey(self, area: str, facts: dict[str, Any], previous: str,
               stage: RequestBudget, cap: int) -> Survey:
        tool = self._terminal()
        agent = AgentTask(role="cartographer", label=f"map/{area}",
                          system=render("cartographer", terminal=tool.name,
                                        previous=bool(previous)),
                          task=self._task(area, facts, previous),
                          tools=[*code_tools(include_git=self.ws.repo is not None), tool],
                          model=self._model(), max_turns=cap + 4, max_requests=cap,
                          priority="background")
        run = self.engine.run(agent, ToolContext(ws=self.ws, index=self.index,
                                                 extra={"agent": f"map/{area}"}), stage)
        s = Survey(area)
        if not run.ok:
            s.error = f"{run.status}: {run.error}"
            return s
        out = run.output or {}
        s.ok, s.summary = True, str(out.get("summary", "")).strip()
        s.modules = [m for m in out.get("modules") or [] if m.get("path")][:15]
        for ep in out.get("entry_points") or []:
            link = self._check(ep.get("at") or {})
            if link is None:
                s.dropped.append(f"entry point: {ep.get('what')}")
            else:
                s.entry_points.append((str(ep.get("what", "")).strip(), link))
        for inv in out.get("invariants") or []:
            links = [x for x in (self._check(raw) for raw in inv.get("evidence") or []) if x]
            if links:
                s.invariants.append((str(inv["statement"]).strip(), links))
            else:
                s.dropped.append(f"invariant: {inv.get('statement')}")
        return s

    def run(self) -> Result:
        started = time.monotonic()
        facts = collect_facts(self.job, self.ws, self.internal)
        previous = self.memory.card() if self.memory else ""
        self.trace.event("map_start", project=facts["project"], sha=facts["sha"],
                         areas=facts["areas"], previous=bool(previous))
        names = facts["areas"]
        stage = self.budget.child("survey", self.budget.cap)
        cap = max(8, min(60, self.budget.cap // max(1, len(names))))
        with ThreadPoolExecutor(max_workers=self.parallel) as pool:
            surveys = list(pool.map(lambda a: self.survey(a, facts, previous, stage, cap), names))
        consumers = self.memory.consumers_of(facts["package"]) \
            if self.memory and facts["package"] else []
        card = render_card(facts, surveys, consumers)
        public = {k: v for k, v in facts.items() if k not in ("churn", "fixes")}
        ok = any(s.ok for s in surveys) or not names
        self.stats.append(StageStat(stage="survey", requests=stage.used, agents=len(names),
                                    seconds=round(time.monotonic() - started, 1),
                                    notes=f"{sum(s.ok for s in surveys)}/{len(names)} areas"))
        self.trace.write_text("card.md", card)
        self.trace.event("map_end", requests=self.budget.used,
                         invariants=sum(len(s.invariants) for s in surveys),
                         dropped=[d for s in surveys for d in s.dropped],
                         failed=[f"{s.area}: {s.error}" for s in surveys if not s.ok])
        updates = [MemoryUpdate(kind="facts", project=facts["project"],
                                content=json.dumps(public, indent=2) + "\n")]
        if ok:
            updates.insert(0, MemoryUpdate(kind="card", project=facts["project"], content=card))
        return Result(job_id=self.job.job_id, run_id=self.run_id, kind="map",
                      status="ok" if ok else "failed",
                      reason="" if ok else "no area could be surveyed",
                      summary=f"mapped {len(names)} area(s) of {facts['project']} at "
                              f"{facts['sha'][:10]}", stats=self.stats,
                      requests=self.budget.used, llm=self.client.stats.as_dict(),
                      memory_commit=self.memory.commit if self.memory else "",
                      memory_updates=updates)


def _where(link: Any) -> str:
    return f"`{link.path}:{link.line}`"


def render_card(facts: dict[str, Any], surveys: list[Survey], consumers: list[dict]) -> str:
    verified = sum(len(s.invariants) for s in surveys)
    dropped = sum(len(s.dropped) for s in surveys)
    lines = [f"# {facts['project']} — repo card", "",
             f"<sub>Mapped at `{(facts['sha'] or '?')[:10]}` on "
             f"{facts['mapped_at'][:10]} by the cartographer · {verified} invariant(s) "
             f"verified against the code, {dropped} unverified item(s) left out.</sub>", ""]
    if facts.get("description"):
        lines += [facts["description"], ""]
    pkg = facts.get("package")
    if pkg or facts.get("pins"):
        lines += ["## Package", ""]
        if pkg:
            lines.append(f"Publishes `{pkg}` {facts.get('version', '')}".rstrip() + ".")
        internal = facts.get("internal_pins") or []
        pins = facts.get("pins") or {}
        shown = internal or list(pins)[:20]
        if shown:
            lines.append("Depends on: " + ", ".join(f"`{n}=={pins.get(n, '?')}`" for n in shown)
                         + ".")
        lines.append("")
    if consumers:
        lines += ["## Consumers", "", "Other projects whose default branch pins this package "
                  "(a change to its API reaches them):"]
        lines += [f"- {c['project']} pins `{pkg}=={(c.get('pins') or {}).get(pkg, '?')}`"
                  for c in consumers[:15]]
        lines.append("")
    lines += ["## How to test", "", f"`{facts['test']}` (from `.habodek.md`)" if facts.get("test")
              else "No recipe in `.habodek.md`; pytest is assumed.", ""]
    lines += ["## Layout", ""]
    for s in surveys:
        label = "top-level files" if s.area == "." else ", ".join(
            f"`{a}/`" for a in s.area.split(","))
        if not s.ok:
            lines.append(f"- {label}: not surveyed ({s.error[:80]})")
            continue
        lines.append(f"- {label}: {s.summary}")
        lines += [f"  - `{m['path']}` — {str(m.get('responsibility', '')).strip()}"
                  for m in s.modules[:10]]
    entries = [(w, link) for s in surveys for w, link in s.entry_points]
    if entries:
        lines += ["", "## Entry points", ""]
        lines += [f"- {w} — {_where(link)} `{link.quote[:100]}`" for w, link in entries[:25]]
    invariants = [(st, links) for s in surveys for st, links in s.invariants]
    if invariants:
        lines += ["", "## Invariants", "", "Checked against the code when mapped; check again "
                  "before relying on one."]
        for st, links in invariants[:30]:
            cites = "; ".join(f"{_where(x)} `{x.quote[:90]}`" for x in links[:3])
            lines.append(f"- {st} — {cites}")
    if facts.get("churn") or facts.get("fixes"):
        lines += ["", "## Hot spots", ""]
        if facts.get("churn"):
            lines.append("Most changed: " + ", ".join(f"`{f}` ({n})" for f, n in facts["churn"])
                         + ".")
        if facts.get("fixes"):
            lines.append("Past fixes: " + "; ".join(facts["fixes"]) + ".")
    text = "\n".join(lines)
    if len(text) > CARD_CHARS:
        text = text[:CARD_CHARS] + "\n... (card cut)"
    return (text + "\n\n## Team notes\n\n" + TEAM_NOTES_START
            + "\n(Notes for the reviewer. This section is kept when the card is rewritten.)\n"
            + TEAM_NOTES_END + "\n")


def run_map(job: MapJob, ws: Workspace, client: LLMClient, trace: TraceWriter,
            memory: MemoryView | None = None, internal: tuple[str, ...] = ()) -> Result:
    carto = Cartographer(job, ws, client, trace, memory, internal)
    try:
        return carto.run()
    except Exception as exc:  # noqa: BLE001 - the control plane needs a result.json
        trace.event("map_failed", error=f"{type(exc).__name__}: {exc}")
        return Result(job_id=job.job_id, run_id=carto.run_id, kind="map", status="failed",
                      reason=f"{type(exc).__name__}: {str(exc)[:500]}",
                      requests=carto.budget.used, llm=client.stats.as_dict())
