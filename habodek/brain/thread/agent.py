"""The thread agent: replies and post-push re-checks, one procedure (design §6.7).

It starts from the stored finding (claim, evidence chain, scenario, the SHA it was
made at), maps every link of the chain through `diff(finding_sha, HEAD)`, and gives
the thread model the developer's words, the diff since, and the chain where it now is.
The model re-walks the chain, goes where the developer points, and concludes
`fixed` / `fix_incomplete` / `stands` / `withdrawn` / `need_info`.

The other model then attacks the conclusion. On disagreement there is one more round
of investigation with the challenger's argument, and a second challenge; if they still
disagree the verdict is `unclear`, with both arguments — never a coin toss. A
conclusion that relaxes the finding (`fixed`, `withdrawn`) fails closed to `unclear`
when the challenge cannot run.

Budget: 30 requests (handoff §3.1), split across the rounds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from habodek.brain.codeintel.files import read_text
from habodek.brain.codeintel.refs import SymbolIndex
from habodek.brain.codeintel.workspace import PathError, Workspace
from habodek.brain.contract import EvidenceLink, Result, StageStat, ThreadJob, ThreadVerdict
from habodek.brain.llm.client import LLMClient
from habodek.brain.runtime.loop import AgentTask, LoopEngine, RunResult
from habodek.brain.runtime.tool import Tool, ToolContext, code_tools
from habodek.brain.thread.linemap import LineMapper
from habodek.brain.trace.writer import TraceWriter, new_run_id
from habodek.brain.workflow import evidence
from habodek.brain.workflow.budget import THREAD_CAP, RequestBudget
from habodek.brain.workflow.comment import where
from habodek.brain.workflow.prompts import render

VERDICTS = ["fixed", "fix_incomplete", "stands", "withdrawn", "need_info"]
_LINK = {"type": "object", "properties": {
    "path": {"type": "string"}, "line": {"type": "integer"},
    "quote": {"type": "string"}, "claim": {"type": "string"}},
    "required": ["path", "line", "quote", "claim"]}

THREAD_SCHEMA = {"type": "object", "properties": {
    "claim_type": {"type": "string", "enum": [
        "fixed", "handled_elsewhere", "by_design", "not_reachable", "question",
        "disagreement", "none", "other"],
        "description": "What the developer claims (none: a push without a message)."},
    "verdict": {"type": "string", "enum": VERDICTS},
    "reason": {"type": "string", "minLength": 10},
    "reply": {"type": "string", "minLength": 10,
              "description": "The reply to post in the thread."},
    "remaining": {"type": "array", "items": _LINK,
                  "description": "For fix_incomplete / stands: each site still broken."},
    "convinced_by": {"type": "array", "items": _LINK,
                     "description": "For withdrawn: the code that convinced you."},
    "lesson": {"type": "string",
               "description": "For withdrawn: what a future review should know."}},
    "required": ["claim_type", "verdict", "reason", "reply"]}

CHALLENGE_SCHEMA = {"type": "object", "properties": {
    "outcome": {"type": "string", "enum": ["agree", "disagree"]},
    "reason": {"type": "string", "minLength": 10},
    "evidence": {"type": "array", "items": _LINK}},
    "required": ["outcome", "reason"]}

# Requests per round, within the thread cap of 30.
CAPS = {"thread": 14, "challenge": 4, "thread_2": 8, "challenge_2": 4}


@dataclass
class Conclusion:
    verdict: str
    reason: str
    reply: str
    remaining: list[EvidenceLink]
    convinced_by: list[EvidenceLink]
    lesson: str
    claim_type: str


class ThreadReview:
    def __init__(self, job: ThreadJob, ws: Workspace, client: LLMClient, trace: TraceWriter,
                 index: SymbolIndex | None = None) -> None:
        self.job, self.ws, self.client, self.trace = job, ws, client, trace
        self.index = index or SymbolIndex(ws)
        self.engine = LoopEngine(client, trace)
        self.budget = RequestBudget(job.request_budget or THREAD_CAP, "thread")
        self.run_id = trace.run_id if trace.run_id != "untraced" else new_run_id("t")
        self.stats: list[StageStat] = []
        diff = ws.git_diff(ws.base_sha, ws.head_sha) if ws.repo and ws.base_sha else ""
        self.diff_text = diff
        self.mapper = LineMapper(diff)

    # ------------------------------------------------------------------ context
    def _model(self, role: str) -> str:
        return self.job.models.roles.get(role) or self.client.profiles.roles.get(role) or role

    def _current_code(self, spec: str, line: int, radius: int = 2) -> str:
        try:
            lines = read_text(self.ws.resolve(spec)).splitlines()
        except (PathError, OSError):
            return "(file not found)"
        lo, hi = max(1, line - radius), min(len(lines), line + radius)
        return "\n".join(f"{n:>6}\t{lines[n - 1]}" for n in range(lo, hi + 1))

    def chain_at_head(self) -> str:
        finding = self.job.finding
        out: list[str] = []
        for i, link in enumerate(finding.chain, 1):
            label = f"{i}. {(link.repo + ':') if link.repo else ''}{link.path}:{link.line} " \
                    f"`{link.quote}` — {link.claim}"
            if link.repo:  # another repository: locate the quote in its current code
                check = evidence.check_quote(self.ws, link)
                if check.ok and check.link is not None:
                    now = f"now at {check.link.path}:{check.link.line} (quote still present)"
                    code = self._current_code(f"{evidence.split_spec(link.path, link.repo)[0]}:"
                                              f"{check.link.path}", check.link.line)
                else:
                    now, code = "the quoted code is no longer there", ""
            else:
                mapped = self.mapper.map(link.path, link.line)
                if mapped.status == "deleted":
                    now, code = "the file was deleted", ""
                else:
                    now = f"now at {mapped.path}:{mapped.line} ({mapped.status} since the " \
                          "finding)"
                    code = self._current_code(mapped.path, mapped.line or 1)
            out.append(f"{label}\n   -> {now}\n{code}")
        return "\n\n".join(out)

    def task(self, extra: str = "") -> str:
        f = self.job.finding
        notes = "\n\n".join(f"**{n.author}**{' (bot)' if n.is_bot else ''}: {n.body}"
                            for n in self.job.notes) or "(no message: the event is a push)"
        diff = self.diff_text or "(nothing changed in this repository since the finding)"
        if len(diff) > 60_000:
            diff = diff[:60_000] + "\n... (diff cut; use git_diff/read for the rest)"
        roots = ", ".join(self.ws.roots(include_base=True))
        parts = [
            f"# Your finding {f.id} ({f.severity}), made at {f.head_sha[:10]}\n\n"
            f"Title: {f.title}\n\nClaim: {f.claim}\n\nFailing scenario: {f.scenario}\n\n"
            f"Proposed fix: {f.fix}\n\nAnchored at {f.anchor.path}:{f.anchor.line}",
            f"# The thread (event: {self.job.event})\n\n{notes}",
            f"# The evidence chain, mapped to the current head {self.ws.head_sha[:10]}\n\n"
            f"{self.chain_at_head()}",
            f"# What changed in this repository since the finding "
            f"(base: = the finding's revision)\n\n```diff\n{diff}\n```",
            f"# Roots you can read\n\n{roots}",
        ]
        if extra:
            parts.append(extra)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ rounds
    def _verified(self, raw: list[dict[str, Any]] | None) -> list[EvidenceLink]:
        out = []
        for item in raw or []:
            check = evidence.check_quote(self.ws, item)
            if check.ok and check.link is not None:
                out.append(check.link)
        return out

    def _thread_tool(self) -> Tool:
        def check(args: dict[str, Any], ctx: ToolContext) -> str | None:
            verdict = args.get("verdict")
            for key in ("remaining", "convinced_by"):
                bad = [evidence.check_quote(self.ws, link).reason for link in args.get(key) or []
                       if not evidence.check_quote(self.ws, link).ok]
                if bad:
                    return f"{key}: these quotes are not in the code: {'; '.join(bad)}."
            if verdict in ("fix_incomplete", "stands") and not args.get("remaining"):
                return f"{verdict} needs `remaining`: each site still broken, with its quote."
            if verdict == "withdrawn" and not args.get("convinced_by"):
                return "withdrawn needs `convinced_by`: the code that convinced you."
            return None

        return Tool("conclude_thread", "Record your conclusion and the reply to post.",
                    THREAD_SCHEMA, terminal=True, check=check)

    def _agent(self, role: str, label: str, system: str, task: str, terminal: Tool,
               cap: int) -> RunResult:
        agent = AgentTask(role=role, label=label, system=system, task=task,
                          tools=[*code_tools(include_git=self.ws.repo is not None), terminal],
                          model=self._model(role), max_turns=cap + 4, max_requests=cap,
                          priority="reply" if self.job.event == "reply" else "recheck")
        started = time.monotonic()
        stage = self.budget.child(label, cap)
        run = self.engine.run(agent, ToolContext(ws=self.ws, index=self.index), stage)
        self.stats.append(StageStat(stage=label, requests=stage.used, agents=1,
                                    seconds=round(time.monotonic() - started, 1),
                                    notes=run.status))
        return run

    def _investigate(self, label: str, cap: int, extra: str = "") -> Conclusion | None:
        tool = self._thread_tool()
        run = self._agent("thread", label, render("thread", terminal=tool.name),
                          self.task(extra), tool, cap)
        if not run.ok:
            self.trace.event("thread_round_failed", label=label, status=run.status,
                             error=run.error)
            return None
        out = run.output or {}
        return Conclusion(out["verdict"], str(out.get("reason", "")), str(out.get("reply", "")),
                          self._verified(out.get("remaining")),
                          self._verified(out.get("convinced_by")),
                          str(out.get("lesson", "") or ""), str(out.get("claim_type", "")))

    def _challenge(self, conclusion: Conclusion, label: str, cap: int) -> tuple[str, str]:
        tool = Tool("challenge", "Agree or disagree with the conclusion.", CHALLENGE_SCHEMA,
                    terminal=True)
        cited = "\n".join(f"- {(link.repo + ':') if link.repo else ''}{link.path}:{link.line} "
                          f"`{link.quote}` — {link.claim}"
                          for link in conclusion.remaining + conclusion.convinced_by)
        extra = (f"# The conclusion to check\n\nVerdict: **{conclusion.verdict}**\n\n"
                 f"Reasoning: {conclusion.reason}\n\nCited:\n{cited or '- (nothing)'}\n\n"
                 f"Reply it would post:\n> {conclusion.reply}")
        run = self._agent("thread_challenger", label,
                          render("thread_challenger", terminal=tool.name),
                          self.task(extra), tool, cap)
        if not run.ok:
            return "error", f"{run.status}: {run.error}"
        out = run.output or {}
        return str(out.get("outcome")), str(out.get("reason", ""))

    # ------------------------------------------------------------------ run
    def run(self) -> Result:
        self.trace.event("thread_start", job=self.job.job_id, finding=self.job.finding.id,
                         event=self.job.event, finding_sha=self.ws.base_sha,
                         head_sha=self.ws.head_sha)
        first = self._investigate("thread/1", CAPS["thread"])
        if first is None:
            return self._result("failed", None, "the thread agent did not conclude")
        outcome, argument = self._challenge(first, "challenge/1", CAPS["challenge"])
        final, unclear_reason = first, ""
        if outcome == "error":
            if first.verdict in ("fixed", "withdrawn"):
                unclear_reason = f"the second model could not check the conclusion ({argument})"
        elif outcome == "disagree":
            second = self._investigate(
                "thread/2", CAPS["thread_2"],
                f"# A second reviewer disagrees with your first conclusion "
                f"({first.verdict})\n\n{argument}\n\nInvestigate their argument in the "
                "code, then conclude again.")
            if second is None:
                unclear_reason = argument
            else:
                outcome2, argument2 = self._challenge(second, "challenge/2", CAPS["challenge_2"])
                if outcome2 == "agree":
                    final = second
                else:
                    final, unclear_reason = second, argument2 or argument
        if unclear_reason:
            body = (f"I could not settle this. My reading: {final.reason}\n\n"
                    f"The counter-argument: {unclear_reason}\n\nA human should decide.")
            verdict = ThreadVerdict(verdict="unclear", body=body, remaining=final.remaining)
            return self._result("ok", verdict, "unclear after challenge")
        return self._result("ok", self._verdict(final), final.verdict)

    def _verdict(self, c: Conclusion) -> ThreadVerdict:
        body = c.reply.strip()
        if c.verdict in ("fix_incomplete", "stands"):
            missing = [link for link in c.remaining if link.path not in body]
            if missing:
                body += "\n\nStill affected:\n" + "\n".join(
                    f"- {where(link)} — `{link.quote[:100]}`" for link in missing)
        lesson = None
        if c.verdict == "withdrawn":
            lesson = c.lesson.strip() or f"Finding withdrawn after the developer's reply: " \
                                         f"{c.reason[:200]}"
        return ThreadVerdict(verdict=c.verdict, body=body, remaining=c.remaining,
                             convinced_by=c.convinced_by, lesson_candidate=lesson)

    def _result(self, status: str, verdict: ThreadVerdict | None, summary: str) -> Result:
        result = Result(job_id=self.job.job_id, run_id=self.run_id, kind="thread",
                        status=status, reason="" if status == "ok" else summary,
                        thread=verdict, summary=summary, stats=self.stats,
                        requests=self.budget.used, llm=self.client.stats.as_dict())
        self.trace.event("thread_end", verdict=verdict.verdict if verdict else None,
                         requests=self.budget.used)
        return result


def run_thread(job: ThreadJob, ws: Workspace, client: LLMClient, trace: TraceWriter) -> Result:
    review = ThreadReview(job, ws, client, trace)
    try:
        return review.run()
    except Exception as exc:  # noqa: BLE001 - the control plane needs a result.json
        trace.event("thread_failed", error=f"{type(exc).__name__}: {exc}")
        return Result(job_id=job.job_id, run_id=review.run_id, kind="thread", status="failed",
                      reason=f"{type(exc).__name__}: {str(exc)[:500]}",
                      requests=review.budget.used, llm=client.stats.as_dict())
