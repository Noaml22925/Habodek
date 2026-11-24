"""The agent loop: one role, one bounded question, native tool calls, a terminal tool.

Small on purpose (design §6.5). It makes a flash model's long session reliable:

- Every structured output is the arguments of the role's terminal tool, validated by
  schema and by the role's own check. Invalid arguments come back as a tool error
  naming the violation; three strikes end the run as `malformed`.
- Guards: an identical repeated call returns "already done"; five identical calls,
  or several turns with no new read or note, bring a checkpoint; a reasoning-only,
  empty or plain-text turn gets a targeted nudge (always a *user* message: Qwen
  rejects a late system message).
- Budgets: turns, requests (charged per HTTP request, through the review's budget)
  and wall clock. Near the end the run is told to conclude and offered only its
  terminal tool, with a small reserve of requests to do it.
- Compaction (compaction.py) at ~60% and ~80% of the model's window.
- Everything goes to the trace.

`Engine` is the seam that keeps other engines (Qwen Code, OpenHands) pluggable
(handoff §3.7).
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol

from habodek.brain.llm.client import BudgetExhausted, GatewayError, LLMClient
from habodek.brain.llm.shaping import ToolSpec, Turn
from habodek.brain.runtime import compaction as cp
from habodek.brain.runtime.tool import Tool, ToolContext, run_tool
from habodek.brain.trace.writer import NullTrace, TraceWriter
from habodek.brain.workflow.budget import RequestBudget

MAX_STRIKES = 3
IDENTICAL_CHECKPOINT = 5
NO_PROGRESS_CHECKPOINT = 4
CONCLUDE_RESERVE = 2  # requests kept back so a run can always conclude


@dataclass
class AgentTask:
    role: str
    label: str  # unique within the job, e.g. "investigate/H3"
    system: str
    task: str  # the first user message: objective + evidence pack
    tools: list[Tool]
    model: str | None = None  # profile name; default: the role's model
    max_turns: int = 40
    max_requests: int = 40
    wall_clock_s: float = 3600.0
    priority: str = "review"
    effort: str | None = None

    @property
    def terminal(self) -> Tool:
        terminals = [t for t in self.tools if t.terminal]
        if len(terminals) != 1:
            raise ValueError(f"{self.label}: exactly one terminal tool required")
        return terminals[0]


@dataclass
class RunResult:
    label: str
    role: str
    status: str  # concluded | malformed | budget | degenerate | error | timeout
    output: dict[str, Any] | None
    model: str
    turns: int = 0
    requests: int = 0
    notes: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)  # "name(args)" per executed call
    guards: list[str] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0
    transcript: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "concluded" and self.output is not None


class Engine(Protocol):
    def run(self, task: AgentTask, ctx: ToolContext,
            budget: RequestBudget | None = None) -> RunResult: ...


# --------------------------------------------------------------------------- texts


def _nudge_plain(terminal: str) -> str:
    return (f"Do not answer in plain text. To finish, call {terminal} with your conclusion. "
            "To continue, call one or more tools (batch independent reads in one turn).")


def _nudge_empty(kind: str, terminal: str) -> str:
    what = "only reasoning and no tool call" if kind == "reasoning_only" else "no output"
    return (f"Your last turn had {what}. Act on your reasoning now: call the tools you need "
            f"(several in one turn if they are independent), or call {terminal} to finish.")


def _checkpoint(reason: str, terminal: str) -> str:
    return (f"Checkpoint ({reason}). Call note with what you have established so far "
            f"(each fact with file:line) and what you will check next. Then either continue "
            f"with a different action, or call {terminal} if you can conclude.")


def _conclude_now(reason: str, terminal: str) -> str:
    return (f"Stop investigating: {reason}. Call {terminal} now with what you have "
            "established. Where something is unverified, say so in the conclusion rather "
            "than guessing.")


def _call_key(name: str, args: dict[str, Any] | None) -> str:
    return name + json.dumps(args or {}, sort_keys=True, ensure_ascii=False)


def _short_call(name: str, args: dict[str, Any] | None) -> str:
    text = json.dumps(args or {}, ensure_ascii=False)
    return f"{name}({text[:160]})"


# --------------------------------------------------------------------------- engine


class LoopEngine:
    def __init__(self, client: LLMClient, trace: TraceWriter | None = None) -> None:
        self.client = client
        self.profiles = client.profiles
        self.trace = trace or client.trace or NullTrace()

    def run(self, task: AgentTask, ctx: ToolContext,
            budget: RequestBudget | None = None) -> RunResult:
        run = _Run(self, task, ctx, budget)
        return run.execute()


class _Run:
    def __init__(self, engine: LoopEngine, task: AgentTask, ctx: ToolContext,
                 budget: RequestBudget | None) -> None:
        self.engine = engine
        self.client = engine.client
        self.trace = engine.trace
        self.task = task
        self.ctx = ctx
        self.terminal = task.terminal
        self.tools = {t.name: t for t in task.tools}
        self.model = task.model or self.engine.profiles.roles.get(task.role) or next(
            iter(self.engine.profiles.profiles))
        cap = task.max_requests
        self.budget = budget.child(task.label, cap) if budget else RequestBudget(cap, task.label)
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": task.system},
            {"role": "user", "content": task.task},
        ]
        self.pinned = 2
        self.result = RunResult(task.label, task.role, "error", None, self.model)
        self.strikes = 0
        self.seen: dict[str, int] = {}
        self.identical: Counter[str] = Counter()
        self.no_progress = 0
        self.concluding = False
        self.conclude_turns = 0
        self.started = time.monotonic()
        self.compaction = cp.CompactionState()

    # ------------------------------------------------------------------ helpers
    def _event(self, kind: str, **fields: Any) -> None:
        self.trace.event(kind, run=self.task.label, role=self.task.role, **fields)

    def _guard(self, what: str) -> None:
        self.result.guards.append(what)
        self._event("guard", guard=what, turn=self.result.turns)

    def _say(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def _specs(self) -> list[ToolSpec]:
        if self.concluding:
            return [self.terminal.spec()]
        return [t.spec() for t in self.task.tools]

    def _enter_conclude(self, reason: str) -> None:
        if not self.concluding:
            self.concluding = True
            self._guard(f"conclude:{reason}")
            self._say(_conclude_now(reason, self.terminal.name))

    def _complete(self, messages: list[dict[str, Any]], specs: list[ToolSpec]) -> Turn:
        turn = self.client.complete(
            self.model, messages, specs, priority=self.task.priority, effort=self.task.effort,
            budget=self.budget, context={"run": self.task.label, "role": self.task.role,
                                         "turn": self.result.turns + 1})
        if turn.model != self.model:
            self._event("run_model_switch", from_model=self.model, to_model=turn.model)
            self.model = turn.model
        return turn

    # ------------------------------------------------------------------ compaction
    def _maybe_compact(self) -> None:
        window = self.engine.profiles.get(self.model).context_window
        used = cp.estimate_tokens(self.messages)
        if used > cp.ELIDE_AT * window:
            n = cp.elide_old_outputs(self.messages, self.pinned, cp.KEEP_RECENT_TOOL_RESULTS)
            if n:
                self.compaction.elisions += n
                self._event("compaction", step="elide", outputs=n, before=used,
                            after=cp.estimate_tokens(self.messages))
                used = cp.estimate_tokens(self.messages)
        if used > cp.SUMMARISE_AT * window:
            self._summarise(used)

    def _summarise(self, used: int) -> None:
        tool = Tool(cp.SUMMARY_TOOL_NAME, "Record your progress before older turns are "
                    "replaced.", cp.SUMMARY_SCHEMA, terminal=True)
        request = [*self.messages, {"role": "user", "content": cp.SUMMARY_PROMPT}]
        summary_args: dict[str, Any] | None = None
        try:
            turn = self._complete(request, [tool.spec()])
            for call in turn.tool_calls:
                if call.name == tool.name and call.valid:
                    summary_args = call.arguments
                    break
        except BudgetExhausted:
            pass
        if summary_args is None:  # mechanical fallback: never lose the notes
            summary_args = {"established": [], "hypotheses": [], "open_questions": [],
                            "files_read": sorted(self.ctx.files_read),
                            "next_step": "re-read what you need and conclude"}
            self._guard("summary_fallback")
        self.messages = cp.rebuild(self.messages, self.pinned,
                                   cp.render_summary(summary_args, self.ctx.notes))
        self.compaction.summaries += 1
        self._event("compaction", step="summarise", before=used,
                    after=cp.estimate_tokens(self.messages))

    # ------------------------------------------------------------------ main
    def execute(self) -> RunResult:
        task = self.task
        self._event("run_start", model=self.model, tools=[t.name for t in task.tools],
                    task_chars=len(task.task), max_turns=task.max_turns,
                    max_requests=task.max_requests)
        try:
            self._loop()
        finally:
            r = self.result
            r.model = self.model
            r.requests = self.budget.used
            r.notes = list(self.ctx.notes)
            r.files_read = sorted(self.ctx.files_read)
            r.seconds = round(time.monotonic() - self.started, 2)
            r.transcript = self.messages
            self._event("run_end", status=r.status, turns=r.turns, requests=r.requests,
                        output=r.output, error=r.error, guards=r.guards,
                        elisions=self.compaction.elisions, summaries=self.compaction.summaries)
        return self.result

    def _loop(self) -> None:
        task, r = self.task, self.result
        while True:
            if not self.concluding:
                if r.turns >= task.max_turns:
                    self._enter_conclude(f"the turn limit ({task.max_turns}) is reached")
                elif time.monotonic() - self.started > task.wall_clock_s:
                    self._enter_conclude("the time limit is reached")
                elif self.budget.remaining <= CONCLUDE_RESERVE:
                    self._enter_conclude("the request budget is nearly spent")
            elif self.conclude_turns >= CONCLUDE_RESERVE:
                r.status, r.error = "budget", "did not conclude when asked to"
                return
            if self.concluding:
                self.conclude_turns += 1
            self._maybe_compact()
            try:
                turn = self._complete(self.messages, self._specs())
            except BudgetExhausted as exc:
                r.status, r.error = "budget", str(exc)
                return
            except GatewayError as exc:
                r.status, r.error = "error", str(exc)
                return
            r.turns += 1
            self.messages.append(turn.to_history())
            if turn.degenerate:
                r.status, r.error = "degenerate", turn.degenerate_reason or ""
                return
            if turn.tool_calls:
                if self._handle_calls(turn):
                    return
            elif turn.kind == "answer":
                self._guard("plain_text")
                self._say(_nudge_plain(self.terminal.name))
                self.no_progress += 1
            else:
                self._guard(turn.kind)
                self._say(_nudge_empty(turn.kind, self.terminal.name))
                self.no_progress += 1
            if self.strikes >= MAX_STRIKES:
                r.status, r.error = "malformed", f"{self.strikes} invalid tool calls"
                return
            if self.no_progress >= NO_PROGRESS_CHECKPOINT and not self.concluding:
                self._guard("no_progress")
                self._say(_checkpoint(f"{self.no_progress} turns without a new read or note",
                                      self.terminal.name))
                self.no_progress = 0

    def _handle_calls(self, turn: Turn) -> bool:
        """Run a turn's calls; True when the run is over."""
        r = self.result
        results: list[tuple[str, str]] = []
        progress = False
        checkpoint = False
        concluded: dict[str, Any] | None = None
        for call in turn.tool_calls:
            tool = self.tools.get(call.name)
            if self.concluding and tool is not None and not tool.terminal:
                results.append((call.id, f"error: only {self.terminal.name} may be called now."))
                continue
            if not call.valid or tool is None:
                self.strikes += 1
                self._event("tool_error", tool=call.name, error=call.error,
                            raw=call.raw_arguments[:2000])
                results.append((call.id, f"error: invalid call to {call.name}: {call.error}. "
                                         "Fix the arguments and call again."))
                continue
            args = call.arguments or {}
            if tool.terminal:
                problem = tool.check(args, self.ctx) if tool.check else None
                if problem:
                    self.strikes += 1
                    self._event("tool_error", tool=call.name, error=problem)
                    results.append((call.id, f"error: {problem} Fix it and call "
                                             f"{tool.name} again."))
                    continue
                concluded = args
                results.append((call.id, "recorded"))
                break
            key = _call_key(call.name, args)
            self.identical[key] += 1
            if key in self.seen:
                results.append((call.id, "already done: an identical call was made in turn "
                                         f"{self.seen[key]}; its result is above (or was "
                                         "elided: then ask for a narrower range). Do something "
                                         "different."))
                if self.identical[key] >= IDENTICAL_CHECKPOINT:
                    checkpoint = True
                continue
            self.seen[key] = r.turns
            before_reads, before_notes = len(self.ctx.files_read), len(self.ctx.notes)
            started = time.monotonic()
            output = run_tool(tool, args, self.ctx)
            r.calls.append(_short_call(call.name, args))
            self._event("tool_call", tool=call.name, args=args, output_chars=len(output),
                        seconds=round(time.monotonic() - started, 3),
                        output=output[:4000])
            if tool.reads or len(self.ctx.files_read) > before_reads or \
                    len(self.ctx.notes) > before_notes:
                progress = True
            results.append((call.id, output))
        answered = {call_id for call_id, _ in results}
        for call in turn.tool_calls:  # every call id gets an answer, always
            if call.id not in answered:
                results.append((call.id, "not run: the run concluded in this turn"))
        for call_id, content in results:
            self.messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        if concluded is not None:
            r.status, r.output = "concluded", concluded
            return True
        self.no_progress = 0 if progress else self.no_progress + 1
        if checkpoint and not self.concluding:
            self._guard("identical_calls")
            self._say(_checkpoint("the same call was repeated several times",
                                  self.terminal.name))
        return False
