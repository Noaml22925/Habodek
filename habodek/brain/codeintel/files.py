"""Reading and searching files, without silent truncation.

`read` returns whole files with line numbers up to 2,000 lines / 100 KB, then pages
and says so. `grep` is ripgrep with full regex; it reports totals when it shows only
part of the matches, and refuses a query so broad its result would be noise, saying
how to narrow it. Nothing is cut from the top, and nothing is cut without a note.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from habodek.brain.codeintel.workspace import PathError, Workspace

MAX_LINES = 2000
MAX_BYTES = 100_000
MAX_TOTAL_BYTES = 250_000  # across the files of one multi-path read
GREP_SHOW = 60
GREP_REFUSE = 2000
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
             ".pytest_cache", ".tox", "dist", "build", ".ruff_cache"}


def _rg() -> str:
    found = shutil.which("rg")
    if not found:
        raise RuntimeError("ripgrep (rg) is not installed")
    return found


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


@dataclass
class ReadResult:
    spec: str
    text: str
    start: int
    end: int
    total_lines: int
    truncated: bool
    error: str | None = None

    def render(self) -> str:
        if self.error:
            return f"== {self.spec}: {self.error}"
        header = f"== {self.spec} (lines {self.start}-{self.end} of {self.total_lines})"
        if self.truncated:
            header += f" -- more below: read with start={self.end + 1}"
        return f"{header}\n{self.text}"


def read_file(ws: Workspace, spec: str, start: int = 1, end: int | None = None,
              budget_bytes: int = MAX_BYTES) -> ReadResult:
    try:
        path = ws.resolve(spec)
    except PathError as exc:
        return ReadResult(spec, "", 0, 0, 0, False, str(exc))
    shown = ws.display(path)
    if path.is_dir():
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir()
                         if p.name not in SKIP_DIRS)
        return ReadResult(shown, "\n".join(entries), 1, len(entries), len(entries), False)
    if not path.exists():
        return ReadResult(shown, "", 0, 0, 0, False, "no such file")
    raw = path.read_bytes()
    if b"\0" in raw[:8000]:
        return ReadResult(shown, "", 0, 0, 0, False, f"binary file ({len(raw)} bytes)")
    lines = raw.decode("utf-8", errors="replace").splitlines()
    total = len(lines)
    start = max(1, start)
    last = min(total, end if end else total, start + MAX_LINES - 1)
    out: list[str] = []
    size = 0
    number = start
    for number in range(start, last + 1):
        line = f"{number:>6}\t{lines[number - 1]}"
        if size + len(line) > budget_bytes and out:
            number -= 1
            break
        out.append(line)
        size += len(line) + 1
    else:
        number = last
    end_line = number if out else start - 1
    requested_end = min(total, end) if end else total
    return ReadResult(shown, "\n".join(out), start, end_line, total, end_line < requested_end)


def read_many(ws: Workspace, specs: list[str], start: int = 1, end: int | None = None) -> str:
    """Read several files in one call (the model is told to batch reads)."""
    parts: list[str] = []
    left = MAX_TOTAL_BYTES
    for i, spec in enumerate(specs):
        if left <= 0:
            rest = ", ".join(specs[i:])
            parts.append(f"== not read (output limit for one call reached): {rest} -- "
                         "read them in another call")
            break
        result = read_file(ws, spec, start, end, budget_bytes=min(MAX_BYTES, left))
        parts.append(result.render())
        left -= len(result.text)
    return "\n\n".join(parts)


# ------------------------------------------------------------------------ grep


@dataclass
class GrepHit:
    spec: str
    line: int
    text: str


@dataclass
class GrepResult:
    pattern: str
    hits: list[GrepHit]
    total_matches: int
    total_files: int
    refused: str | None = None
    error: str | None = None

    def render(self) -> str:
        if self.error:
            return f"grep {self.pattern!r}: {self.error}"
        if self.refused:
            return self.refused
        if not self.hits:
            return f"grep {self.pattern!r}: no matches"
        lines = [f"{h.spec}:{h.line}: {h.text}" for h in self.hits]
        head = f"grep {self.pattern!r}: {self.total_matches} matches in {self.total_files} files"
        if len(self.hits) < self.total_matches:
            head += (f", showing {len(self.hits)} -- narrow with path_glob or a more specific "
                     "pattern to see the rest")
        return head + "\n" + "\n".join(lines)


def grep(ws: Workspace, pattern: str, *, roots: list[str] | None = None,
         path_glob: str | None = None, ignore_case: bool = False, fixed: bool = False,
         word: bool = False, show: int = GREP_SHOW, refuse_over: int = GREP_REFUSE) -> GrepResult:
    if not pattern:
        return GrepResult(pattern, [], 0, 0, error="empty pattern")
    labels = roots or ["head"]
    hits: list[GrepHit] = []
    files: set[str] = set()
    total = 0
    for label in labels:
        try:
            root = ws.root(label)
        except PathError as exc:
            return GrepResult(pattern, [], 0, 0, error=str(exc))
        cmd = [_rg(), "--json", "--no-config", "--max-columns", "300", "--max-columns-preview"]
        for skip in SKIP_DIRS:
            cmd += ["--glob", f"!{skip}"]
        if path_glob:
            cmd += ["--glob", path_glob]
        if ignore_case:
            cmd.append("-i")
        if fixed:
            cmd.append("-F")
        if word:
            cmd.append("-w")
        cmd += ["-e", pattern, str(root)]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode not in (0, 1):
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            return GrepResult(pattern, [], 0, 0, error=(err[-1] if err else "ripgrep failed"))
        for raw in proc.stdout.splitlines():
            event = json.loads(raw)
            if event.get("type") != "match":
                continue
            data = event["data"]
            path_text = data["path"].get("text") or ""
            spec = ws.display(Path(path_text))
            text = (data["lines"].get("text") or "").rstrip("\n")
            n = len(data.get("submatches") or [1])
            total += n
            files.add(spec)
            if len(hits) < show:
                hits.append(GrepHit(spec, data["line_number"], text.strip()[:300]))
    if total > refuse_over:
        return GrepResult(pattern, [], total, len(files), refused=(
            f"grep {pattern!r}: {total} matches in {len(files)} files -- too broad to be useful. "
            "Narrow it: add path_glob (e.g. 'src/payments/**'), use word=true, or search for a "
            "longer, more specific pattern such as 'name(' or 'def name'."))
    hits.sort(key=lambda h: (h.spec, h.line))
    return GrepResult(pattern, hits, total, len(files))


# ------------------------------------------------------------------------ glob


def glob(ws: Workspace, pattern: str, root: str = "head", limit: int = 500) -> str:
    base = ws.root(root)
    matches: list[str] = []
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base)
        if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file():
            continue
        posix = rel.as_posix()
        if fnmatch.fnmatch(posix, pattern) or (pattern.startswith("**/") and
                                                fnmatch.fnmatch(posix, pattern[3:])):
            matches.append(ws.display(path))
    if not matches:
        return f"glob {pattern!r}: no files"
    shown = matches[:limit]
    head = f"glob {pattern!r}: {len(matches)} files"
    if len(matches) > limit:
        head += f", showing {limit} -- narrow the pattern"
    return head + "\n" + "\n".join(shown)
