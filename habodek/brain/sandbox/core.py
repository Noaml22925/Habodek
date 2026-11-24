"""Sandbox backends and the run log the review and thread agents share.

Request and response are the same JSON for both backends (and for the Go service):

    request  {"root": "head", "cmd": "python -m pytest -q tests/x.py",
              "files": {"tests/test_probe.py": "..."}, "timeout_s": 120}
    response {"exit_code": 1, "timed_out": false, "seconds": 3.2, "output": "...",
              "truncated": false, "env": {"key": "...", "ok": true, "cached": true},
              "error": ""}

`root` is a label (`head`, `base`, `repo:<name>`), never a host path: the backend maps
it to the directory it was given for the job. `files` are written into the throwaway
copy before the command runs (a reproduction test, a probe script); paths must stay
inside the copy.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import httpx

from habodek.brain.sandbox import envprep

MAX_OUTPUT = 200_000  # characters kept from a run's combined stdout+stderr
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
RECIPE_FILE = ".habodek.md"
SKIP_COPY = {".git", ".hg", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
             ".pytest_cache", ".tox", ".ruff_cache"}


class SandboxError(RuntimeError):
    """The sandbox could not run the command (as opposed to the command failing)."""


@dataclass
class RunRequest:
    root: str
    cmd: str
    files: dict[str, str] = field(default_factory=dict)
    timeout_s: int = DEFAULT_TIMEOUT

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunOutcome:
    exit_code: int
    timed_out: bool = False
    seconds: float = 0.0
    output: str = ""
    truncated: bool = False
    env: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RunOutcome:
        return cls(exit_code=int(data.get("exit_code", -1)),
                   timed_out=bool(data.get("timed_out")), seconds=float(data.get("seconds", 0)),
                   output=str(data.get("output", "")), truncated=bool(data.get("truncated")),
                   env=dict(data.get("env") or {}), error=str(data.get("error", "")))


def recipe_for(head: Path, base: Path | None) -> dict[str, list[str]]:
    """The repository's sandbox recipe: `.habodek.md` from the target branch (base) if it
    has one, else from head. Read from the target branch so an MR cannot change how it
    is itself tested."""
    for root in (base, head):
        if root is not None and (Path(root) / RECIPE_FILE).is_file():
            return envprep.read_recipe(str(Path(root) / RECIPE_FILE))
    return {}


class Sandbox(Protocol):
    name: str

    def run(self, request: RunRequest) -> RunOutcome: ...


def check_files(files: dict[str, str]) -> None:
    for rel in files:
        p = PurePosixPath(rel)
        if not rel or p.is_absolute() or ".." in p.parts or rel.startswith("~"):
            raise SandboxError(f"file path {rel!r} must be relative, inside the copy")


def clamp_timeout(value: Any) -> int:
    try:
        seconds = int(value or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        seconds = DEFAULT_TIMEOUT
    return max(5, min(seconds, MAX_TIMEOUT))


def cap_output(text: str, limit: int = MAX_OUTPUT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    half = limit // 2
    return text[:half] + "\n... output cut ...\n" + text[-half:], True


# --------------------------------------------------------------------------- HTTP


class HttpSandbox:
    """The control plane's sandbox: `POST {base}/run` with the job's token."""

    name = "control-plane"

    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None,
                 timeout_margin_s: float = 900.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        # A first run may include env-prep (pip install): allow for it beyond the command.
        self._http = httpx.Client(transport=transport, timeout=MAX_TIMEOUT + timeout_margin_s)

    def run(self, request: RunRequest) -> RunOutcome:
        check_files(request.files)
        request.timeout_s = clamp_timeout(request.timeout_s)
        try:
            resp = self._http.post(f"{self.base_url}/run", json=request.as_json(),
                                   headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as exc:
            raise SandboxError(f"sandbox unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise SandboxError(f"sandbox refused ({resp.status_code}): {resp.text[:300]}")
        return RunOutcome.from_json(resp.json())


# --------------------------------------------------------------------------- local


def local_available() -> bool:
    """Can this machine run commands in a user + network namespace?"""
    if not shutil.which("unshare") or sys.platform != "linux":
        return False
    try:
        proc = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "true"],
                              capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


class LocalSandbox:
    """Standalone runs: env-prep on the host, the command in `unshare --net`.

    Isolation: a private network namespace (no interface but a downed loopback, so not
    even the host's 127.0.0.1 is reachable), a private PID namespace, a throwaway copy
    of the root, a cleared environment, address-space and process limits, a timeout.
    The filesystem is the host's: this backend is for scenario runs on our own
    fixtures. Production uses the control plane's containers.
    """

    name = "local"

    def __init__(self, roots: dict[str, Path], cache_dir: Path, *, recipe_root: str = "base",
                 index_url: str = "", find_links: tuple[str, ...] = (),
                 python: str | None = None, memory_mb: int = 4096) -> None:
        self.roots = {k: Path(v) for k, v in roots.items()}
        self.cache_dir = Path(cache_dir)
        self.recipe_root = recipe_root if recipe_root in self.roots else "head"
        self.index_url, self.find_links = index_url, tuple(find_links)
        self.python = python or sys.executable
        self.memory_mb = memory_mb
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------- env-prep
    def recipe_path(self) -> Path | None:
        for label in (self.recipe_root, "head"):
            path = self.roots[label] / RECIPE_FILE if label in self.roots else None
            if path is not None and path.is_file():
                return path
        return None

    def recipe(self) -> dict[str, list[str]]:
        path = self.recipe_path()
        return envprep.read_recipe(str(path)) if path else {}

    def _tag(self) -> str:
        return f"local:{self.python}:{sys.version_info.major}.{sys.version_info.minor}"

    def prepare(self, root: str) -> dict[str, Any]:
        src = self._root(root)
        recipe_path = self.recipe_path()
        recipe_text = recipe_path.read_text(errors="replace") if recipe_path else ""
        key = envprep.cache_key(str(src), recipe_text, self._tag())
        venv = self.cache_dir / "venvs" / key
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            self.cache_dir.joinpath("venvs").mkdir(parents=True, exist_ok=True)
            with open(self.cache_dir / "venvs" / f"{key}.lock", "w") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)  # several brain processes share the cache
                marker = venv / envprep.MARKER
                if marker.is_file():
                    return {"key": key, "ok": True, "cached": True, "venv": str(venv)}
                shutil.rmtree(venv, ignore_errors=True)
                ok, summary = envprep.build(str(src), str(venv), envprep.parse_recipe(
                    recipe_text), self.index_url, self.find_links, python=self.python)
        env = {"key": key, "ok": ok, "cached": False, "venv": str(venv),
               "installed": summary.get("installed", [])}
        if not ok:
            env["log"] = summary.get("log", [])[-15:]
        return env

    def _root(self, label: str) -> Path:
        if label not in self.roots:
            raise SandboxError(f"unknown root {label!r}; runnable roots: "
                               f"{', '.join(sorted(self.roots))}")
        return self.roots[label]

    # ------------------------------------------------------------- run
    def run(self, request: RunRequest) -> RunOutcome:
        check_files(request.files)
        timeout = clamp_timeout(request.timeout_s)
        src = self._root(request.root)
        env_info = self.prepare(request.root)
        if not env_info["ok"]:
            log = "\n".join(env_info.get("log", []))
            return RunOutcome(exit_code=-1, env=env_info,
                              error=f"env-prep failed for {request.root}:\n{log}")
        with tempfile.TemporaryDirectory(prefix="habodek-run-") as tmp:
            work = Path(tmp) / "work"
            shutil.copytree(src, work, symlinks=True,
                            ignore=lambda d, names: [n for n in names if n in SKIP_COPY])
            for rel, text in request.files.items():
                target = work / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            home = Path(tmp) / "home"
            home.mkdir()
            venv = Path(env_info["venv"])
            env = {"PATH": f"{venv / 'bin'}:/usr/local/bin:/usr/bin:/bin", "HOME": str(home),
                   "LANG": "C.UTF-8", "PYTHONPATH": f"{work}:{work / 'src'}",
                   "PYTHONDONTWRITEBYTECODE": "1", "VIRTUAL_ENV": str(venv),
                   "TMPDIR": str(Path(tmp)), "CI": "1"}
            argv = ["unshare", "--user", "--map-root-user", "--net", "--pid", "--fork",
                    "--kill-child", "prlimit", f"--as={self.memory_mb * 1024 * 1024}",
                    "--nofile=4096", "--", "sh", "-c", request.cmd]
            started = time.monotonic()
            timed_out = False
            proc = subprocess.Popen(argv, cwd=work, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            try:
                raw, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, 9)
                raw, _ = proc.communicate()
            output, truncated = cap_output(raw.decode(errors="replace"))
            return RunOutcome(exit_code=proc.returncode if not timed_out else -9,
                              timed_out=timed_out,
                              seconds=round(time.monotonic() - started, 2), output=output,
                              truncated=truncated, env=env_info)


# --------------------------------------------------------------------------- log


@dataclass
class LoggedRun:
    id: str
    request: RunRequest
    outcome: RunOutcome
    agent: str = ""


class RunLog:
    """Every run of a job, by id, so a finding can cite one as its witness.

    Thread-safe: investigators run in parallel. The log is written to the trace.
    """

    def __init__(self, sandbox: Sandbox, trace: Any = None, max_runs: int = 40,
                 recipe: dict[str, list[str]] | None = None,
                 roots: tuple[str, ...] = ("head", "base")) -> None:
        self.sandbox = sandbox
        self.trace = trace
        self.max_runs = max_runs
        self.recipe = recipe or {}
        self.roots = roots
        self.runs: dict[str, LoggedRun] = {}
        self._lock = threading.Lock()

    def run(self, request: RunRequest, agent: str = "") -> LoggedRun:
        with self._lock:
            if len(self.runs) >= self.max_runs:
                raise SandboxError(f"run limit reached ({self.max_runs} runs for this job)")
            run_id = f"run{len(self.runs) + 1}"
            self.runs[run_id] = None  # type: ignore[assignment] - reserve the id
        try:
            outcome = self.sandbox.run(request)
        except SandboxError:
            with self._lock:
                del self.runs[run_id]
            raise
        logged = LoggedRun(run_id, request, outcome, agent)
        with self._lock:
            self.runs[run_id] = logged
        if self.trace is not None:
            self.trace.event("sandbox_run", id=run_id, agent=agent, root=request.root,
                             cmd=request.cmd, files=sorted(request.files),
                             exit_code=outcome.exit_code, timed_out=outcome.timed_out,
                             seconds=outcome.seconds, env=outcome.env, error=outcome.error)
            self.trace.write_text(f"runs/{run_id}.txt", _run_record(logged))
        return logged

    def get(self, run_id: str) -> LoggedRun | None:
        with self._lock:
            return self.runs.get(run_id)


def _run_record(run: LoggedRun) -> str:
    parts = [f"$ ({run.request.root}) {run.request.cmd}",
             f"exit {run.outcome.exit_code}  {run.outcome.seconds}s"
             + ("  TIMED OUT" if run.outcome.timed_out else ""),
             json.dumps(run.outcome.env)]
    for rel, text in run.request.files.items():
        parts += [f"--- file {rel}", text]
    parts += ["--- output", run.outcome.output or run.outcome.error]
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------- selection


def sandbox_from_env(env: dict[str, str], roots: dict[str, Path], cache_dir: Path,
                     index_url: str = "", find_links: tuple[str, ...] = ()) -> Sandbox | None:
    """`HABODEK_SANDBOX_URL` (control plane) wins; `HABODEK_SANDBOX=local` opts in to the
    local backend; otherwise there is no sandbox and agents get no `run` tool."""
    url = env.get("HABODEK_SANDBOX_URL", "")
    if url:
        token = env.get("HABODEK_SANDBOX_TOKEN") or env.get("HABODEK_LLM_TOKEN", "")
        return HttpSandbox(url, token)
    mode = env.get("HABODEK_SANDBOX", "").lower()
    if mode == "local" or (mode == "auto" and local_available()):
        return LocalSandbox(roots, cache_dir, index_url=index_url, find_links=find_links)
    return None
