"""Evaluation cases: a known change with known flaws planted in it.

A case is a tiny two-commit repository. `before/` is committed as the base,
`after/` as the head, and the reviewer under test is pointed at the diff between
them. Because the case materialises as a *real* git repo, the harness exercises
the same `build_change_map` path production uses -- the evaluation cannot pass on
a diff the real system would parse differently.

Cases with an empty `expect` list are the important ones: a clean change where the
correct answer is silence. Without them a scorer only measures recall, and a
reviewer that flags everything scores perfectly.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ExpectedFinding(BaseModel):
    """A flaw deliberately planted in the change."""

    id: str
    category: str
    severity: str
    file: str
    # Inclusive anchor window. A reviewer that points anywhere in here has found
    # it -- demanding an exact line would punish reasonable disagreement about
    # whether to flag the `try` or the `except`.
    lines: list[int] = Field(min_length=2, max_length=2)
    summary: str
    # At least one must appear in the finding's text for it to count as a match,
    # so that a finding landing on the right line for the wrong reason is not
    # scored as a hit.
    keywords: list[str] = Field(default_factory=list)

    @property
    def line_range(self) -> range:
        return range(self.lines[0], self.lines[1] + 1)

    def covers(self, start: int, end: int) -> bool:
        return not (end < self.lines[0] or start > self.lines[1])


class Case(BaseModel):
    """One evaluation scenario."""

    id: str
    title: str
    language: str = "python"
    commit_message: str = "Update"
    expect: list[ExpectedFinding] = Field(default_factory=list)
    # Findings that are reasonable but not required -- they are neither rewarded
    # as hits nor punished as noise.
    tolerate: list[str] = Field(default_factory=list)
    notes: str = ""

    @property
    def is_clean(self) -> bool:
        """A change with nothing wrong with it. The correct review is empty."""
        return not self.expect


@dataclass(slots=True)
class MaterialisedCase:
    case: Case
    repo: Path
    base: str
    head: str


def load_case(case_dir: Path) -> Case:
    raw = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))
    return Case.model_validate(raw)


def discover_cases(corpus_dir: Path) -> list[Case]:
    cases = [
        load_case(child)
        for child in sorted(corpus_dir.iterdir())
        if child.is_dir() and (child / "case.yaml").is_file()
    ]
    return cases


def case_dir(corpus_dir: Path, case_id: str) -> Path:
    for child in sorted(corpus_dir.iterdir()):
        if child.is_dir() and (child / "case.yaml").is_file() and load_case(child).id == case_id:
            return child
    raise FileNotFoundError(f"no case with id {case_id!r} in {corpus_dir}")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _copy_tree(src: Path, dst: Path) -> None:
    for item in src.rglob("*"):
        if item.is_file():
            target = dst / item.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def materialise(source: Path, workdir: Path) -> MaterialisedCase:
    """Build the two-commit repository for a case.

    `before/` becomes the base commit and `after/` the head, so the diff the
    reviewer sees is exactly the planted change.
    """
    case = load_case(source)
    repo = workdir / case.id
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "eval@habodek.local")
    _git(repo, "config", "user.name", "Habodek Eval")
    # Keep line endings identical across platforms, or the diff differs on Windows.
    _git(repo, "config", "core.autocrlf", "false")

    _copy_tree(source / "before", repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    # Deletions matter: a file present in before/ and absent in after/ must show
    # up as a deletion, so the tree is rebuilt rather than overlaid.
    for item in list(repo.iterdir()):
        if item.name != ".git":
            shutil.rmtree(item) if item.is_dir() else item.unlink()
    _copy_tree(source / "after", repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", case.commit_message)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    return MaterialisedCase(case=case, repo=repo, base=base, head=head)
