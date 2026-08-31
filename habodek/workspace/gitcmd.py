"""Thin, safe wrapper around the git CLI.

Every git invocation in Habodek goes through here so that argument handling,
encoding, and error reporting stay consistent -- and so the agent's read-only
tools can never reach a git subcommand that writes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr


def run_git(
    repo: Path,
    *args: str,
    check: bool = True,
    timeout: float = 120.0,
) -> str:
    """Run a git command in `repo` and return stdout.

    Decoding is lenient: real repositories contain files in mixed encodings and a
    UnicodeDecodeError in the middle of a diff is never worth aborting a review for.
    """
    cmd = ["git", "--no-pager", *args]
    proc = subprocess.run(
        cmd,
        cwd=str(repo),
        capture_output=True,
        timeout=timeout,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise GitError(list(args), proc.returncode, stderr)
    return stdout


def rev_parse(repo: Path, rev: str) -> str:
    return run_git(repo, "rev-parse", rev).strip()


def merge_base(repo: Path, a: str, b: str) -> str:
    return run_git(repo, "merge-base", a, b).strip()


def is_git_repo(path: Path) -> bool:
    try:
        run_git(path, "rev-parse", "--git-dir")
        return True
    except (GitError, FileNotFoundError, NotADirectoryError):
        return False


def repo_root(path: Path) -> Path:
    return Path(run_git(path, "rev-parse", "--show-toplevel").strip())
