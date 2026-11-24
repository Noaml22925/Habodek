"""The `run` tool: execute a command in the sandbox, and cite the run as a witness.

Offered to investigators, verifiers and the thread agent only when a sandbox is
configured. A run's id (`run3`) can be cited in a conclusion's `witnesses`; the
conclusion is checked against the run log, so a model cannot cite a run that did not
happen, or misstate its exit code (the witness is rebuilt from the log).
"""

from __future__ import annotations

from typing import Any

from habodek.brain.contract import RunWitness
from habodek.brain.runtime.tool import Tool, ToolContext
from habodek.brain.sandbox.core import RunLog, RunRequest, SandboxError

TOOL_OUTPUT = 12_000  # characters of a run's output shown to the model
WITNESS_TAIL = 1_500  # characters of output kept on the finding

WITNESSES_SCHEMA = {
    "type": "array",
    "description": "Sandbox runs that demonstrate the conclusion, by id (e.g. 'run2'), "
                   "each with what it shows. Optional; only runs you made.",
    "items": {"type": "object", "properties": {
        "run": {"type": "string"},
        "shows": {"type": "string", "description": "What this run demonstrates."}},
        "required": ["run", "shows"]}}


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "..." + text[-limit:]


def render_outcome(run_id: str, request: RunRequest, outcome: Any) -> str:
    if outcome.error and outcome.exit_code == -1:
        return f"{run_id} ({request.root}): could not run: {outcome.error}"
    head = f"{run_id} ({request.root}): exit {outcome.exit_code} in {outcome.seconds}s"
    if outcome.timed_out:
        head += f" — TIMED OUT after {request.timeout_s}s"
    env = outcome.env or {}
    if env and not env.get("cached", True):
        head += " (dependencies installed for this revision first)"
    body = outcome.output.strip() or "(no output)"
    if len(body) > TOOL_OUTPUT:
        body = _tail(body, TOOL_OUTPUT)
        head += f"\n(showing the last {TOOL_OUTPUT} characters)"
    return f"{head}\n{body}"


def run_tool(log: RunLog) -> Tool:
    test = (log.recipe.get("test") or [""])[0]
    hint = (f"This repository's test command (from its recipe): `{test}`." if test else
            "No test recipe: pytest is usually available (`python -m pytest -q path`).")
    roots = list(log.roots)

    def run(args: dict[str, Any], ctx: ToolContext) -> str:
        request = RunRequest(root=args.get("root") or "head", cmd=args["cmd"],
                             files=dict(args.get("files") or {}),
                             timeout_s=int(args.get("timeout_s") or 120))
        try:
            logged = log.run(request, agent=str(ctx.extra.get("agent", "")))
        except SandboxError as exc:
            return f"error: {exc}"
        ctx.extra.setdefault("runs", []).append(logged.id)
        return render_outcome(logged.id, request, logged.outcome)

    return Tool(
        "run",
        "Run a shell command in a throwaway sandbox copy of a revision: the repository's "
        "dependencies are installed, there is no network, nothing persists between runs. "
        "Use it to settle a question by execution: run the relevant tests, or add a small "
        "reproduction (a test file or script, via `files`) and run it. A test that fails "
        "at head and passes at base is strong evidence; a passing test refutes only what "
        "it actually exercises. Runs take time, not requests: prefer one targeted run "
        f"over many. {hint} Roots: {', '.join(roots)}.",
        {"type": "object", "properties": {
            "cmd": {"type": "string", "minLength": 1},
            "root": {"type": "string", "enum": roots},
            "files": {"type": "object", "additionalProperties": {"type": "string"},
                      "description": "Files to add to the copy first: {relative path: "
                                     "content}."},
            "timeout_s": {"type": "integer", "minimum": 5, "maximum": 600}},
         "required": ["cmd"], "additionalProperties": False},
        run, reads=True)


def check_witnesses(log: RunLog | None, raw: list[dict[str, Any]] | None) -> str | None:
    if not raw:
        return None
    if log is None:
        return "witnesses cite sandbox runs, but no sandbox is available: leave them out."
    unknown = [str(w.get("run")) for w in raw if log.get(str(w.get("run"))) is None]
    if unknown:
        made = ", ".join(sorted(k for k, v in log.runs.items() if v)) or "none"
        return f"no such run: {', '.join(unknown)} (runs made so far: {made})."
    return None


def witnesses_from(log: RunLog | None, raw: list[dict[str, Any]] | None) -> list[RunWitness]:
    """Rebuild cited witnesses from the run log: the facts come from the log, only the
    `shows` text from the model."""
    out: list[RunWitness] = []
    if log is None:
        return out
    for item in raw or []:
        logged = log.get(str(item.get("run")))
        if logged is None or any(w.id == logged.id for w in out):
            continue
        out.append(RunWitness(id=logged.id, root=logged.request.root, cmd=logged.request.cmd,
                              files=sorted(logged.request.files),
                              exit_code=logged.outcome.exit_code,
                              timed_out=logged.outcome.timed_out,
                              output_tail=_tail(logged.outcome.output.strip(), WITNESS_TAIL),
                              shows=str(item.get("shows", ""))[:300]))
    return out
