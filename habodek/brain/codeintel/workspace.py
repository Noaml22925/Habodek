"""The code a job can see, and how a tool names a place in it.

A workspace has named roots:

- `head`   — the reviewed repository at the MR head (the default: bare paths mean this)
- `base`   — the same repository at the merge base
- `dep:<name>@<version>` — an internal dependency's source at a pinned version
- `repo:<name>` — another repository (a consumer, for reverse dependencies)
- `site:<n>` — a venv's site-packages

A *path spec* is `"<root>:<relative path>"`, or a bare relative path for `head`. Every
tool result prints paths in this form, so the model can feed them straight back.

Git history (log, blame, show, diff) runs against `repo`, a repository that holds
both revisions. In production the control plane prepares the worktrees; for local
runs `Workspace.from_repo` creates them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from habodek.workspace.diff import ChangeMap, build_change_map
from habodek.workspace.gitcmd import GitError, run_git


class PathError(ValueError):
    """A path spec that does not resolve inside the workspace."""


@dataclass
class Workspace:
    head: Path
    repo: Path | None = None
    base: Path | None = None
    base_sha: str = ""
    head_sha: str = ""
    extra_roots: dict[str, Path] = field(default_factory=dict)  # dep:/repo:/site: roots
    work_dir: Path | None = None  # where lazily created worktrees go
    _change_map: ChangeMap | None = field(default=None, repr=False)

    # ------------------------------------------------------------- construction
    @classmethod
    def from_repo(cls, repo: Path | str, base: str, head: str, work_dir: Path | str) -> Workspace:
        """Worktrees for `head` and the merge base of `base...head` from a local repo."""
        repo = Path(repo).resolve()
        work_dir = Path(work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        head_sha = run_git(repo, "rev-parse", head).strip()
        base_sha = run_git(repo, "merge-base", base, head).strip()
        ws = cls(head=work_dir / "head", repo=repo, base_sha=base_sha, head_sha=head_sha,
                 work_dir=work_dir)
        _add_worktree(repo, ws.head, head_sha)
        ws.ensure_base()
        return ws

    def ensure_base(self) -> Path | None:
        if self.base is not None or not (self.repo and self.base_sha and self.work_dir):
            return self.base
        target = self.work_dir / "base"
        _add_worktree(self.repo, target, self.base_sha)
        self.base = target
        return target

    def add_root(self, label: str, path: Path | str) -> None:
        if not label.startswith(("dep:", "repo:", "site:")):
            raise ValueError(f"root label must start with dep:, repo: or site: ({label!r})")
        self.extra_roots[label] = Path(path).resolve()

    # ------------------------------------------------------------- roots and paths
    def roots(self, include_base: bool = False) -> dict[str, Path]:
        out = {"head": self.head.resolve()}
        if include_base and self.base:
            out["base"] = self.base.resolve()
        out.update(self.extra_roots)
        return out

    def root(self, label: str) -> Path:
        if label == "head":
            return self.head.resolve()
        if label == "base":
            base = self.ensure_base()
            if base is None:
                raise PathError("the base revision is not available in this workspace")
            return base.resolve()
        if label in self.extra_roots:
            return self.extra_roots[label]
        raise PathError(f"unknown root {label!r}; known: {', '.join(self.roots(True))}")

    def split(self, spec: str) -> tuple[str, str]:
        spec = spec.strip()
        for prefix in ("head:", "base:"):
            if spec.startswith(prefix):
                return prefix[:-1], spec[len(prefix):].lstrip("/")
        if spec.startswith(("dep:", "repo:", "site:")):
            for label in sorted(self.extra_roots, key=len, reverse=True):
                if spec == label or spec.startswith(label + "/") or spec.startswith(label + ":"):
                    return label, spec[len(label):].lstrip("/:")
            raise PathError(f"unknown root in {spec!r}; known: {', '.join(self.roots(True))}")
        return "head", spec.lstrip("/")

    def resolve(self, spec: str) -> Path:
        label, rel = self.split(spec)
        root = self.root(label)
        path = (root / rel).resolve() if rel and rel != "." else root
        if path != root and root not in path.parents:
            raise PathError(f"{spec!r} is outside the workspace")
        return path

    def display(self, path: Path | str) -> str:
        """The path spec for an absolute path (the inverse of `resolve`)."""
        path = Path(path).resolve()
        best: tuple[int, str, Path] | None = None
        for label, root in self.roots(include_base=True).items():
            if path == root or root in path.parents:
                depth = len(root.parts)
                if best is None or depth > best[0]:
                    best = (depth, label, root)
        if best is None:
            return str(path)
        _, label, root = best
        rel = path.relative_to(root).as_posix() if path != root else "."
        return rel if label == "head" else f"{label}:{rel}"

    # ------------------------------------------------------------- the change
    def change_map(self) -> ChangeMap:
        if self._change_map is None:
            if not self.repo:
                raise GitError(["diff"], 1, "no repository in this workspace")
            self._change_map = build_change_map(self.repo, self.base_sha, self.head_sha)
        return self._change_map

    def changed_paths(self) -> list[str]:
        return [f.path for f in self.change_map().files]

    # ------------------------------------------------------------- git history
    def _git(self, *args: str) -> str:
        if not self.repo:
            raise GitError(list(args), 1, "no repository in this workspace")
        return run_git(self.repo, *args)

    def git_log(self, path: str | None = None, max_count: int = 20, rev: str = "") -> str:
        args = ["log", f"--max-count={max_count}", "--date=short",
                "--format=%h %ad %an  %s", rev or self.head_sha or "HEAD"]
        if path:
            args += ["--", self.split(path)[1]]
        return self._git(*args)

    def git_log_lines(self, path: str, start: int, end: int, max_count: int = 10) -> str:
        """History of a line range (`git log -L`), newest first, patches trimmed."""
        rel = self.split(path)[1]
        out = self._git("log", f"--max-count={max_count}", "--date=short",
                        "--format=commit %h %ad %an  %s", f"-L{start},{end}:{rel}",
                        self.head_sha or "HEAD")
        return out

    def git_blame(self, path: str, start: int, end: int, rev: str = "") -> str:
        rel = self.split(path)[1]
        return self._git("blame", "--date=short", "-L", f"{start},{end}", rev or self.head_sha
                         or "HEAD", "--", rel)

    def git_show(self, rev: str, path: str | None = None) -> str:
        if path:
            return self._git("show", f"{rev}:{self.split(path)[1]}")
        return self._git("show", "--stat", "--patch", "--format=commit %H%nAuthor: %an%nDate: "
                         "%ad%n%n%B", rev)

    def git_diff(self, rev_from: str, rev_to: str, path: str | None = None) -> str:
        args = ["diff", "--no-color", "-M", rev_from, rev_to]
        if path:
            args += ["--", self.split(path)[1]]
        return self._git(*args)


def _add_worktree(repo: Path, target: Path, sha: str) -> None:
    if target.exists() and any(target.iterdir()):
        current = run_git(target, "rev-parse", "HEAD").strip()
        if current == sha:
            return
        raise GitError(["worktree", "add"], 1, f"{target} exists at {current}, wanted {sha}")
    target.parent.mkdir(parents=True, exist_ok=True)
    run_git(repo, "worktree", "add", "--detach", str(target), sha)


def is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([path.resolve(), root.resolve()]) == str(root.resolve())
    except ValueError:
        return False
