"""The memory repository (`habodek-memory`): markdown in git, one directory per project.

    projects/<path_with_namespace>/card.md          the repo card (the cartographer's)
    projects/<path_with_namespace>/facts.json       mechanical facts: package, pins, sha
    projects/<path_with_namespace>/lessons/<id>.md  lessons from verified thread outcomes

Readable, diffable and correctable by a person (design §6.8). Jobs read it **at a
commit** (`MemoryView`), so every run records exactly what it knew and a replay sees
the same memory. Writes go through `apply_updates`, which the control plane runs
after a job under one lock; it merges lessons into the current HEAD, not into the
commit the job read, so concurrent jobs do not lose each other's lessons.

Lessons (only from verified outcomes — a `withdrawn` verdict that survived the
challenge) carry their citations and a count. A new lesson is a `candidate`; a second
independent citation makes it `active`. Only active lessons are put in agents'
context; candidates are reachable through `recall`. A person may set `status:` to
`active` or `retired` by hand; the merge keeps that decision.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from habodek.brain.contract import LessonEvent, MemoryUpdate

AUTHOR = ["-c", "user.name=Habodek", "-c", "user.email=habodek@localhost",
          "-c", "commit.gpgsign=false"]
README = """# habodek-memory

What Habodek knows about each repository it reviews. One directory per GitLab
project under `projects/`:

- `card.md` — the repo card, rewritten by the cartographer. Edit only the
  "Team notes" section: it is kept when the card is rewritten.
- `facts.json` — mechanical facts (package name, internal pins, mapped commit).
- `lessons/*.md` — lessons from verified thread outcomes. Set `status: retired` to
  stop one being used, or `status: active` to use a candidate now.
"""
TEAM_NOTES_START = "<!-- habodek:team-notes -->"
TEAM_NOTES_END = "<!-- /habodek:team-notes -->"
ACTIVE_AT = 2  # independent citations that make a candidate lesson active
MATCH_AT = 0.5  # token Jaccard similarity that makes two lessons the same lesson
_CITE = re.compile(r"<!-- cite:(\S+) -->")
_WORD = re.compile(r"[a-z0-9_]{3,}")
_STOP = {"the", "and", "for", "that", "this", "with", "from", "are", "not", "but", "its",
         "into", "which", "when", "every", "repo", "repository", "here"}


def project_dir(project: str) -> str:
    return "projects/" + project.strip("/")


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


# --------------------------------------------------------------------------- lessons


@dataclass
class Lesson:
    id: str
    text: str
    status: str = "candidate"  # candidate | active | retired
    count: int = 1
    scope: str = ""
    created: str = ""
    updated: str = ""
    citations: list[str] = field(default_factory=list)  # rendered lines, with a cite marker
    path: str = ""

    @property
    def cited(self) -> set[str]:
        return {m for line in self.citations for m in _CITE.findall(line)}

    @property
    def active(self) -> bool:
        return self.status == "active"

    def render(self) -> str:
        head = "\n".join(f"{k}: {v}" for k, v in (
            ("id", self.id), ("status", self.status), ("count", self.count),
            ("scope", self.scope or "/"), ("created", self.created), ("updated", self.updated)))
        cites = "\n".join(self.citations)
        return f"---\n{head}\n---\n\n{self.text.strip()}\n\n## Citations\n\n{cites}\n"

    def brief(self) -> str:
        where = f" (in `{self.scope}`)" if self.scope else ""
        return f"- {self.text.strip()}{where} — seen {self.count}×, lesson `{self.id}`"


def parse_lesson(text: str, path: str = "") -> Lesson | None:
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    body, _, cites = m.group(2).partition("## Citations")
    try:
        count = int(meta.get("count", "1"))
    except ValueError:
        count = 1
    scope = meta.get("scope", "").strip("/")
    return Lesson(id=meta.get("id") or Path(path).stem, text=body.strip(),
                  status=meta.get("status", "candidate").lower(), count=count, scope=scope,
                  created=meta.get("created", ""), updated=meta.get("updated", ""),
                  citations=[ln for ln in cites.strip().splitlines() if ln.strip()], path=path)


def _citation_line(event: LessonEvent) -> tuple[str, str]:
    c = event.citation
    key = f"{c.mr}#{c.finding_id}"
    shown = "; ".join(f"`{x.path}:{x.line}` `{x.quote.strip()[:80]}`" for x in c.convinced_by[:3])
    line = (f"- {c.mr}, finding `{c.finding_id}`: {c.verdict}"
            + (f" at {c.head_sha[:10]}" if c.head_sha else "") + (f" ({c.at})" if c.at else "")
            + (f" — convinced by {shown}" if shown else "") + f" <!-- cite:{key} -->")
    return key, line


def merge_lesson(existing: list[Lesson], event: LessonEvent, today: str) -> Lesson:
    """The lesson the event belongs to, updated; or a new candidate."""
    key, line = _citation_line(event)
    best, score = None, 0.0
    for lesson in existing:
        s = similarity(lesson.text, event.text)
        if s > score:
            best, score = lesson, s
    if best is not None and score >= MATCH_AT:
        if key not in best.cited:
            best.citations.append(line)
            best.count = len(best.cited)
            best.updated = today
        if best.status == "candidate" and best.count >= ACTIVE_AT:
            best.status = "active"
        return best
    lid = "l-" + hashlib.sha1(event.text.strip().lower().encode()).hexdigest()[:8]
    return Lesson(id=lid, text=event.text.strip(), scope=event.scope.strip("/"), created=today,
                  updated=today, citations=[line], count=1)


# --------------------------------------------------------------------------- read


class MemoryView:
    """The memory of one project at one commit (read-only)."""

    def __init__(self, repo: Path | str, project: str, commit: str | None = None) -> None:
        self.repo = Path(repo)
        self.project = project
        self.dir = project_dir(project)
        head = _git(self.repo, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
        self.commit = (commit or head).strip()

    @property
    def empty(self) -> bool:
        return not self.commit

    def read(self, path: str) -> str | None:
        if self.empty:
            return None
        proc = subprocess.run(["git", "-C", str(self.repo), "show", f"{self.commit}:{path}"],
                              capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 else None

    def files(self, prefix: str = "projects/") -> list[str]:
        if self.empty:
            return []
        out = _git(self.repo, "ls-tree", "-r", "--name-only", self.commit, "--", prefix,
                   check=False)
        return [p for p in out.splitlines() if p]

    def card(self) -> str:
        return self.read(f"{self.dir}/card.md") or ""

    def facts(self, project: str | None = None) -> dict:
        text = self.read(f"{project_dir(project or self.project)}/facts.json")
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {}

    def lessons(self) -> list[Lesson]:
        out = []
        for path in self.files(f"{self.dir}/lessons/"):
            if path.endswith(".md"):
                lesson = parse_lesson(self.read(path) or "", path)
                if lesson:
                    out.append(lesson)
        return out

    def active_lessons(self, changed_paths: list[str] | None = None,
                       limit: int = 8) -> list[Lesson]:
        """Active lessons whose scope covers a changed path (all of them if none given)."""
        def applies(lesson: Lesson) -> bool:
            if not lesson.scope or changed_paths is None:
                return True
            return any(p == lesson.scope or p.startswith(lesson.scope + "/") for p in changed_paths)

        chosen = [x for x in self.lessons() if x.active and applies(x)]
        return sorted(chosen, key=lambda x: (-x.count, x.id))[:limit]

    def all_facts(self) -> list[dict]:
        out = []
        for path in self.files():
            if path.endswith("/facts.json"):
                try:
                    out.append(json.loads(self.read(path) or "{}"))
                except json.JSONDecodeError:
                    continue
        return out

    def consumers_of(self, package: str) -> list[dict]:
        """Projects whose default branch pins `package` (the reverse-dependency index)."""
        norm = package.lower().replace("_", "-")
        return [f for f in self.all_facts()
                if norm in {k.lower().replace("_", "-") for k in (f.get("pins") or {})}
                and f.get("project") != self.project]


# --------------------------------------------------------------------------- write


def init_repo(repo: Path | str) -> Path:
    repo = Path(repo)
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _git(repo, "init", "-q", "-b", "main")
    if not _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).strip():
        (repo / "README.md").write_text(README)
        _git(repo, "add", "README.md")
        _git(repo, *AUTHOR, "commit", "-q", "-m", "Start habodek-memory")
    return repo


def _keep_team_notes(new: str, old: str) -> str:
    """The card's Team notes are people's: carry them over when the card is rewritten."""
    def block(text: str) -> str | None:
        i, j = text.find(TEAM_NOTES_START), text.find(TEAM_NOTES_END)
        return text[i + len(TEAM_NOTES_START):j] if 0 <= i < j else None

    kept = block(old)
    if kept is None or TEAM_NOTES_START not in new:
        return new
    i, j = new.find(TEAM_NOTES_START), new.find(TEAM_NOTES_END)
    return new[:i + len(TEAM_NOTES_START)] + kept + new[j:]


def apply_updates(repo: Path | str, updates: list[MemoryUpdate], message: str,
                  today: str | None = None) -> str:
    """Apply updates to the working tree of the memory repository and commit them.

    Returns the new HEAD (unchanged when nothing changed). Serialised by a file lock,
    so two control-plane processes cannot interleave.
    """
    repo = init_repo(repo)
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")
    with open(repo / ".git" / "habodek.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for u in updates:
            pdir = repo / project_dir(u.project)
            pdir.mkdir(parents=True, exist_ok=True)
            if u.kind == "card":
                old = (pdir / "card.md").read_text() if (pdir / "card.md").is_file() else ""
                (pdir / "card.md").write_text(_keep_team_notes(u.content, old))
            elif u.kind == "facts":
                (pdir / "facts.json").write_text(u.content)
            elif u.kind == "lesson" and u.lesson is not None:
                ldir = pdir / "lessons"
                ldir.mkdir(exist_ok=True)
                existing = [x for x in (parse_lesson(p.read_text(), str(p))
                                        for p in sorted(ldir.glob("*.md"))) if x]
                lesson = merge_lesson(existing, u.lesson, today)
                target = Path(lesson.path) if lesson.path else ldir / f"{lesson.id}.md"
                target.write_text(lesson.render())
        _git(repo, "add", "-A", "projects")
        if _git(repo, "status", "--porcelain", "--", "projects").strip():
            _git(repo, *AUTHOR, "commit", "-q", "-m", message)
        return _git(repo, "rev-parse", "HEAD").strip()
