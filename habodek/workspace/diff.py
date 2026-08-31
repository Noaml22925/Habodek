"""Parse a unified diff into a ChangeMap.

This module is the foundation of the whole system, for one reason: a review
comment can only be posted on a line GitLab believes is part of the diff, and
GitLab identifies that line by its *old* and *new* line numbers. So the parser's
real job is not to render a diff -- it is to remember, for every single line,
which side(s) of the change it exists on.

The diff is computed locally from a clone rather than fetched from the GitLab
API, which sidesteps the API's diff size limits and pagination entirely.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from habodek.workspace.gitcmd import run_git

FileStatus = Literal["added", "modified", "deleted", "renamed"]
LineKind = Literal["context", "added", "removed"]
Side = Literal["new", "old"]

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_DIFF_HEADER_RE = re.compile(r'^diff --git ("(?:[^"\\]|\\.)*"|\S+) ("(?:[^"\\]|\\.)*"|\S+)$')


@dataclass(slots=True)
class DiffLine:
    kind: LineKind
    text: str
    old_lineno: int | None
    new_lineno: int | None


@dataclass(slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    heading: str
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def new_range(self) -> range:
        return range(self.new_start, self.new_start + self.new_count)


@dataclass(slots=True)
class FileChange:
    old_path: str
    new_path: str
    status: FileStatus
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def path(self) -> str:
        """The path to show a human: the new one, unless the file was deleted."""
        return self.old_path if self.status == "deleted" else self.new_path

    @property
    def added_lines(self) -> list[DiffLine]:
        return [ln for h in self.hunks for ln in h.lines if ln.kind == "added"]

    @property
    def removed_lines(self) -> list[DiffLine]:
        return [ln for h in self.hunks for ln in h.lines if ln.kind == "removed"]

    @property
    def changed_line_count(self) -> int:
        return len(self.added_lines) + len(self.removed_lines)

    def anchorable(self) -> set[tuple[Side, int]]:
        """Every (side, line number) pair a discussion may legally be attached to.

        Added lines anchor on the new side, removed lines on the old side, and
        context lines on either. `gitlab/anchor.py` consults this before posting so
        that a finding pointing at unchanged code is re-anchored or demoted to the
        summary note instead of being rejected by the API.
        """
        out: set[tuple[Side, int]] = set()
        for hunk in self.hunks:
            for line in hunk.lines:
                if line.new_lineno is not None and line.kind in ("added", "context"):
                    out.add(("new", line.new_lineno))
                if line.old_lineno is not None and line.kind in ("removed", "context"):
                    out.add(("old", line.old_lineno))
        return out

    def line_text(self, side: Side, lineno: int) -> str | None:
        for hunk in self.hunks:
            for line in hunk.lines:
                got = line.new_lineno if side == "new" else line.old_lineno
                if got == lineno:
                    return line.text
        return None

    def window(self, side: Side, lineno: int, radius: int = 1) -> list[str]:
        """Text of the lines around `lineno` on `side`, used for fingerprinting."""
        texts: list[str] = []
        for hunk in self.hunks:
            for line in hunk.lines:
                got = line.new_lineno if side == "new" else line.old_lineno
                if got is not None and abs(got - lineno) <= radius:
                    texts.append(line.text)
        return texts


@dataclass(slots=True)
class ChangeMap:
    """Everything known about what a merge request changes."""

    base_sha: str
    head_sha: str
    files: list[FileChange] = field(default_factory=list)
    title: str = ""
    description: str = ""
    commit_subjects: list[str] = field(default_factory=list)
    source_branch: str = ""
    target_branch: str = ""

    @property
    def total_changed_lines(self) -> int:
        return sum(f.changed_line_count for f in self.files)

    def by_path(self, path: str) -> FileChange | None:
        for f in self.files:
            if path in (f.path, f.new_path, f.old_path):
                return f
        return None


def _dequote(path: str) -> str:
    """Undo git's C-style quoting of paths containing unusual bytes."""
    if not (path.startswith('"') and path.endswith('"') and len(path) >= 2):
        return path
    body = path[1:-1]
    try:
        return body.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return body


def _strip_prefix(path: str) -> str:
    if path == "/dev/null":
        return ""
    if len(path) > 2 and path[0] in ("a", "b") and path[1] == "/":
        return path[2:]
    return path


def parse_unified_diff(text: str) -> list[FileChange]:
    """Parse `git diff` output.

    Tolerant by design -- an unrecognised header line is skipped rather than
    raising, because one odd file must not sink a whole review.
    """
    files: list[FileChange] = []
    current: FileChange | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0
    rename_from: str | None = None
    rename_to: str | None = None

    def finish(f: FileChange | None, r_from: str | None, r_to: str | None) -> None:
        if f is None:
            return
        if f.status == "renamed":
            f.old_path = f.old_path or (r_from or "")
            f.new_path = f.new_path or (r_to or "")
        if not f.new_path and f.status != "deleted":
            f.new_path = f.old_path
        if not f.old_path and f.status != "added":
            f.old_path = f.new_path

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            finish(current, rename_from, rename_to)
            current = FileChange(old_path="", new_path="", status="modified")
            files.append(current)
            hunk = None
            rename_from = rename_to = None
            # Fallback paths; the ---/+++ lines below are authoritative when present.
            match = _DIFF_HEADER_RE.match(raw)
            if match:
                current.old_path = _strip_prefix(_dequote(match.group(1)))
                current.new_path = _strip_prefix(_dequote(match.group(2)))
            continue

        if current is None:
            continue

        if raw.startswith("new file mode"):
            current.status = "added"
            continue
        if raw.startswith("deleted file mode"):
            current.status = "deleted"
            continue
        if raw.startswith("rename from "):
            rename_from = _dequote(raw[len("rename from ") :])
            current.status = "renamed"
            continue
        if raw.startswith("rename to "):
            rename_to = _dequote(raw[len("rename to ") :])
            current.status = "renamed"
            continue
        if raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
            current.is_binary = True
            continue
        if raw.startswith("--- "):
            path = _strip_prefix(_dequote(raw[4:]))
            if path:
                current.old_path = path
            continue
        if raw.startswith("+++ "):
            path = _strip_prefix(_dequote(raw[4:]))
            if path:
                current.new_path = path
            continue

        match = _HUNK_RE.match(raw)
        if match:
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            hunk = Hunk(old_start, old_count, new_start, new_count, match.group(5).strip())
            current.hunks.append(hunk)
            old_no, new_no = old_start, new_start
            continue

        if hunk is None:
            continue

        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            hunk.lines.append(DiffLine("added", raw[1:], None, new_no))
            new_no += 1
        elif raw.startswith("-"):
            hunk.lines.append(DiffLine("removed", raw[1:], old_no, None))
            old_no += 1
        elif raw.startswith(" ") or raw == "":
            hunk.lines.append(DiffLine("context", raw[1:] if raw else "", old_no, new_no))
            old_no += 1
            new_no += 1

    finish(current, rename_from, rename_to)
    return files


def is_ignored(path: str, globs: list[str]) -> bool:
    normalised = path.replace("\\", "/")
    for pattern in globs:
        if fnmatch.fnmatch(normalised, pattern):
            return True
        # `**/x` should also match a bare `x` at the repo root.
        if pattern.startswith("**/") and fnmatch.fnmatch(normalised, pattern[3:]):
            return True
    return False


def build_change_map(
    repo: Path,
    base: str,
    head: str,
    *,
    ignore_globs: list[str] | None = None,
    context_lines: int = 3,
) -> ChangeMap:
    """Diff `base...head` -- against their merge base, matching MR semantics."""
    diff_text = run_git(
        repo,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "-M",
        f"--unified={context_lines}",
        f"{base}...{head}",
    )
    files = parse_unified_diff(diff_text)

    if ignore_globs:
        files = [f for f in files if not is_ignored(f.path, ignore_globs)]
    files = [f for f in files if not f.is_binary]

    subjects = [
        line
        for line in run_git(
            repo, "log", "--no-merges", "--format=%s", f"{base}..{head}"
        ).splitlines()
        if line.strip()
    ]

    return ChangeMap(
        base_sha=run_git(repo, "rev-parse", base).strip(),
        head_sha=run_git(repo, "rev-parse", head).strip(),
        files=files,
        commit_subjects=subjects,
    )
