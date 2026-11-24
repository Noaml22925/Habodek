"""Replay a run, and probe it with a stated hypothesis (design §6.9, handoff M11).

    habodek-brain replay RUN_DIR [--out DIR] [--model ROLE=MODEL] [--from-stage STAGE]
    habodek-brain probe  RUN_DIR --hypothesis "..." [--changed-line PATH::CODE] [--verify]

**Workspace.** `run.json` names every root with the commit it was at; the worktrees
themselves are long gone (the control plane deletes them after a job), so replay
creates them again from those SHAs. A root that cannot be rebuilt is reported.

**Cassette.** The run's recorded traffic — the brain's `events.jsonl` (every request
and response) or the control plane's `llm-proxy.jsonl` — answers any request the
harness makes again. Requests are matched on the conversation's decisions: model,
system prompt, task, every assistant turn, every nudge, the tools offered — but not
tool results, which carry timings. A pure replay that misses means the harness
changed behaviour; the first miss is reported with its role and turn.

**Live from a stage.** `--model ROLE=MODEL` changes a role's model; `--from-stage S`
sends that stage and every later one to the live gateway (`HABODEK_LLM_BASE_URL`)
even when the cassette could answer. Everything before comes from the cassette, so a
stage can be compared under another model without paying for the rest.

**Sandbox runs** are served from the run's recorded runs (`runs/<id>.txt`) when the
same command runs on the same root; anything else runs in the local sandbox if there
is one.

**Probe** runs one investigator on a hypothesis a person states, in the run's
workspace with the run's evidence pack, and says whether the original run had raised
it — splitting a miss into "nobody thought of it" and "it was investigated badly".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from habodek.brain.contract import JobEnvelope
from habodek.brain.memory.store import similarity
from habodek.brain.sandbox.core import (  # noqa: F401 - RunRequest re-exported
    LocalSandbox,
    RunOutcome,
    RunRequest,
    local_available,
)
from habodek.brain.trace.runinfo import read_run  # noqa: F401 - re-exported

STAGES = ("understand", "hypothesise", "investigate", "verify", "judge")
ROLE_STAGE = {"understand": "understand", "finder": "hypothesise", "investigator": "investigate",
              "verifier": "verify", "judge": "judge", "thread": "investigate",
              "cartographer": "investigate"}
CASSETTE_URL = "http://cassette.invalid/v1"


class ReplayError(RuntimeError):
    pass


# --------------------------------------------------------------------------- workspace


def _git(repo: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def rebuild_workspace(info: dict[str, Any], work: Path) -> tuple[Any, Any, list[str]]:
    """The run's job with its roots pointing at worktrees rebuilt from the recorded SHAs."""
    job = JobEnvelope.model_validate({"job": info["job"]}).job
    notes: list[str] = []
    paths: dict[str, str] = {}
    for label, root in (info.get("roots") or {}).items():
        path, sha, repo = root.get("path", ""), root.get("sha"), root.get("repo")
        if path and Path(path).is_dir() and (not sha or _git(path, "rev-parse", "HEAD")
                                             .stdout.strip() == sha):
            paths[label] = path
            continue
        if sha and repo and Path(repo).exists():
            target = work / "roots" / re.sub(r"[^A-Za-z0-9_.@-]", "_", label)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                proc = _git(repo, "worktree", "add", "-q", "--detach", str(target), sha)
                if proc.returncode != 0:
                    why = proc.stderr.strip()[:120]
                    notes.append(f"{label}: cannot check out {sha[:10]}: {why}")
                    continue
            paths[label] = str(target)
            notes.append(f"{label}: rebuilt at {sha[:10]}")
        else:
            notes.append(f"{label}: missing and not rebuildable ({path})")
    if "head" not in paths:
        raise ReplayError("the head worktree cannot be rebuilt: " + "; ".join(notes))
    spec = job.workspace
    spec.head = paths["head"]
    spec.base = paths.get("base")
    spec.other_repos = {k[5:]: v for k, v in paths.items() if k.startswith("repo:")}
    deps = []
    for d in spec.dependencies:
        label = f"dep:{d.name}@{d.version}"
        if label in paths:
            d.path = paths[label]
            deps.append(d)
        elif Path(d.path).is_dir():
            deps.append(d)
    spec.dependencies = deps
    spec.venvs = [p for p in spec.venvs if Path(p).is_dir()]
    from habodek.brain.jobs import workspace_from_job

    ws = workspace_from_job(job)
    ws.work_dir = work
    return job, ws, notes


# --------------------------------------------------------------------------- cassette


def _system_role(body: dict[str, Any]) -> str:
    msgs = body.get("messages") or []
    first = str(msgs[0].get("content", "")) if msgs else ""
    line = first.splitlines()[0] if first else ""
    return line.removeprefix("Role: ").strip()


def request_key(body: dict[str, Any]) -> str:
    """What identifies a request across runs: everything but tool results' text."""
    msgs = []
    for m in body.get("messages") or []:
        m = dict(m)
        if m.get("role") == "tool":
            m["content"] = "<tool result>"
        msgs.append(m)
    keyed = {"model": body.get("model"), "messages": msgs,
             "tools": sorted(t.get("function", {}).get("name", "")
                             for t in body.get("tools") or [])}
    return hashlib.sha256(json.dumps(keyed, sort_keys=True, default=str).encode()).hexdigest()


def load_cassette(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    tape: dict[str, list[dict[str, Any]]] = defaultdict(list)
    events = run_dir / "events.jsonl"
    if events.is_file():
        requests: dict[int, dict[str, Any]] = {}
        for line in events.read_text().splitlines():
            e = json.loads(line) if line else {}
            if e.get("kind") == "llm_request" and isinstance(e.get("body"), dict):
                requests[e["seq"]] = e["body"]
            elif (e.get("kind") == "llm_response" and e.get("status") == 200
                  and e.get("request_seq") in requests and isinstance(e.get("body"), dict)):
                tape[request_key(requests[e["request_seq"]])].append(e["body"])
    proxy = run_dir / "llm-proxy.jsonl"
    if not tape and proxy.is_file():
        for line in proxy.read_text().splitlines():
            rec = json.loads(line) if line else {}
            if rec.get("status") == 200 and isinstance(rec.get("request"), dict) and \
                    isinstance(rec.get("response"), dict):
                tape[request_key(rec["request"])].append(rec["response"])
    return tape


@dataclass
class Tally:
    hits: int = 0
    live: int = 0
    misses: list[str] = field(default_factory=list)  # "role, turn n"


class CassetteTransport(httpx.BaseTransport):
    def __init__(self, tape: dict[str, list[dict[str, Any]]], live_url: str = "",
                 live_from: str | None = None, live: httpx.BaseTransport | None = None) -> None:
        self.tape = {k: list(v) for k, v in tape.items()}
        self.live_url = live_url.rstrip("/")
        self.live_stages = set(STAGES[STAGES.index(live_from):]) if live_from else set()
        self.live = live or (httpx.HTTPTransport() if live_url else None)
        self.tally = Tally()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        role = _system_role(body)
        stage = ROLE_STAGE.get(role.split(" ")[0], "investigate")
        key = request_key(body)
        if stage not in self.live_stages and self.tape.get(key):
            self.tally.hits += 1
            return httpx.Response(200, json=self.tape[key].pop(0))
        turn = sum(1 for m in body.get("messages") or [] if m.get("role") == "assistant") + 1
        if self.live is None:
            self.tally.misses.append(f"{role}, turn {turn}")
            return httpx.Response(409, json={"error": {"message": "replay: no recorded response "
                                                       f"for this request ({role}, turn {turn})"}})
        self.tally.live += 1
        url = self.live_url + "/chat/completions"
        forwarded = httpx.Request("POST", url, content=request.content,
                                  headers={k: v for k, v in request.headers.items()
                                           if k.lower() not in ("host", "content-length")})
        return self.live.handle_request(forwarded)


# --------------------------------------------------------------------------- sandbox


class RecordedSandbox:
    """Serves the run's recorded sandbox runs; falls back to a real sandbox if any."""

    name = "recorded"

    def __init__(self, run_dir: Path, fallback: Any = None) -> None:
        self.fallback = fallback
        self.recorded: dict[tuple, list[RunOutcome]] = defaultdict(list)
        for path in sorted((run_dir / "runs").glob("run*.txt"),
                           key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)):
            text = path.read_text()
            m = re.match(r"\$ \((\S+)\) (.*?)\nexit (-?\d+)\s+([\d.]+)s(  TIMED OUT)?\n(.*?)\n",
                         text, re.DOTALL)
            if not m:
                continue
            files = {}
            for fm in re.finditer(r"--- file (\S+)\n(.*?)(?=\n--- )", text, re.DOTALL):
                files[fm.group(1)] = fm.group(2)
            output = text.split("--- output\n", 1)[1] if "--- output\n" in text else ""
            env = json.loads(m.group(6)) if m.group(6).startswith("{") else {}
            outcome = RunOutcome(exit_code=int(m.group(3)), seconds=float(m.group(4)),
                                 timed_out=bool(m.group(5)), output=output.rstrip("\n"), env=env)
            self.recorded[(m.group(1), m.group(2), tuple(sorted(files.items())))].append(outcome)

    def run(self, request: RunRequest) -> RunOutcome:
        key = (request.root, request.cmd, tuple(sorted(request.files.items())))
        if self.recorded.get(key):
            return self.recorded[key].pop(0)
        if self.fallback is not None:
            return self.fallback.run(request)
        return RunOutcome(exit_code=-1, error="replay: this run was not recorded and there is "
                                              "no sandbox here")


def replay_sandbox(info: dict[str, Any], run_dir: Path, ws: Any, cache: Path) -> Any:
    if info.get("sandbox", "none") == "none":
        return None
    roots = {"head": ws.head, **({"base": ws.base} if ws.base else {}),
             **{k: v for k, v in ws.extra_roots.items() if k.startswith("repo:")}}
    fallback = LocalSandbox(roots, cache) if local_available() else None
    return RecordedSandbox(run_dir, fallback)


# --------------------------------------------------------------------------- probe


def original_outcome(run_dir: Path, claim: str) -> str:
    """Did the original run raise a hypothesis like this one, and what became of it?"""
    path = run_dir / "findings.json"
    records = json.loads(path.read_text()) if path.is_file() else []
    best, score = None, 0.0
    for r in records:
        s = similarity(claim, f"{r.get('title', '')} {r.get('claim', '')}")
        if s > score:
            best, score = r, s
    if best is None or score < 0.25:
        return (f"originally: not hypothesised (nothing similar among {len(records)} "
                "hypotheses): the finders missed it")
    where = "posted" if best.get("status") == "posted" else (
        f"{best.get('status')} at {best.get('dropped_at')}: {str(best.get('drop_reason'))[:80]}")
    name = best.get("hypothesis") or best.get("id")
    return f"originally: {name} {where} (similarity {score:.2f})"


def live_url() -> str:
    return os.environ.get("HABODEK_LLM_BASE_URL", "")
