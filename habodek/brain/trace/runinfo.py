"""`run.json`: a run's inputs, so it can be explained, replayed and probed later.

The job file, every workspace root with the commit it was at (worktrees are deleted
after a job; replay rebuilds them from these SHAs), the brain version, the model
roles, a hash of every role prompt, the memory commit and the sandbox backend.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from habodek.brain import __version__
from habodek.brain.workflow.prompts import ROLES_DIR

RUN_FILE = "run.json"


def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def root_info(path: Path) -> dict[str, str]:
    info = {"path": str(path)}
    sha = _git(path, "rev-parse", "HEAD")
    if sha:
        common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        info["sha"] = sha
        # The repository a worktree belongs to (its .git dir's parent for a normal repo).
        repo = Path(common)
        info["repo"] = str(repo.parent if repo.name == ".git" else repo)
    return info


def prompt_hashes() -> dict[str, str]:
    return {p.stem: hashlib.sha1(p.read_bytes()).hexdigest()[:12]
            for p in sorted(ROLES_DIR.glob("*.md"))}


def write_run_json(trace: Any, job: Any, ws: Any, roles: dict[str, str],
                   memory_commit: str = "", sandbox: str = "none") -> dict[str, Any]:
    roots = {"head": root_info(ws.head)}
    if ws.base is not None:
        roots["base"] = root_info(ws.base)
    for label, path in ws.extra_roots.items():
        roots[label] = root_info(path)
    data = {
        "run_id": trace.run_id, "brain_version": __version__, "kind": job.kind,
        "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "job": json.loads(job.model_dump_json()), "roots": roots, "roles": roles,
        "prompts": prompt_hashes(), "memory_commit": memory_commit, "sandbox": sandbox,
    }
    trace.write_json(RUN_FILE, data)
    return data


def read_run(run_dir: Path | str) -> dict[str, Any]:
    path = Path(run_dir) / RUN_FILE
    if not path.is_file():
        raise FileNotFoundError(f"{path}: not a run directory (no {RUN_FILE})")
    return json.loads(path.read_text())
