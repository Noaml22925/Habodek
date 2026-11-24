"""Evidence: packs built by code, and the mechanical checks that make the review fail closed.

Requests are scarce and tokens are free (handoff §3.1), so every agent starts with an
*evidence pack* assembled deterministically instead of discovering it one tool call at
a time: the MR, the diff, the symbol-level change map, the full text of each changed
function at base and head, every reference to the changed symbols with its enclosing
function, the library diff of a bumped internal dependency, and a repo map.

The checks (design §6.6 stage 7) never trust a model's line number:
- every quote in a chain must exist at its path and revision (the line is corrected
  when the quote sits a few lines away; the link is rejected when it is nowhere);
- the comment's anchor is resolved *from the quoted changed code* against the diff;
- a finding must name the changed line that causes it, and carry evidence beyond it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from habodek.brain.codeintel import pydeps
from habodek.brain.codeintel.changemap import SymbolChanges
from habodek.brain.codeintel.files import read_text
from habodek.brain.codeintel.refs import SymbolIndex
from habodek.brain.codeintel.repomap import build_repo_map
from habodek.brain.codeintel.workspace import PathError, Workspace
from habodek.brain.contract import Anchor, EvidenceLink
from habodek.brain.runtime.compaction import CHARS_PER_TOKEN

ROOT_LABEL = re.compile(r"^(base|head|dep:[^:]+|repo:[^:]+|site:[^:]+):")


# --------------------------------------------------------------------------- packs


@dataclass
class Section:
    title: str
    body: str
    priority: int  # lower survives the cap first

    def render(self) -> str:
        return f"## {self.title}\n\n{self.body.rstrip()}\n"


@dataclass
class EvidencePack:
    sections: list[Section] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    def add(self, title: str, body: str, priority: int) -> None:
        if body and body.strip():
            self.sections.append(Section(title, body, priority))

    def render(self, max_tokens: int) -> str:
        budget = int(max_tokens * CHARS_PER_TOKEN)
        chosen: list[Section] = []
        used = 0
        for section in sorted(self.sections, key=lambda s: s.priority):
            size = len(section.body) + len(section.title) + 8
            if used + size > budget:
                room = budget - used
                if room > 2000 and section.priority <= 2:
                    cut = section.body[: room - 200]
                    chosen.append(Section(section.title, cut + "\n... (cut to fit the evidence "
                                          "budget; read the files for the rest)",
                                          section.priority))
                    used = budget
                else:
                    self.dropped.append(section.title)
                continue
            chosen.append(section)
            used += size
        order = {id(s): i for i, s in enumerate(self.sections)}
        chosen.sort(key=lambda s: order.get(id(s), len(order)))
        text = "\n".join(s.render() for s in chosen)
        if self.dropped:
            text += ("\n(Left out to fit the evidence budget, available through the tools: "
                     + "; ".join(self.dropped) + ")\n")
        return text


def _numbered(lines: list[str], start: int, end: int) -> str:
    return "\n".join(f"{n:>6}\t{lines[n - 1]}" for n in range(start, min(end, len(lines)) + 1))


def function_text(ws: Workspace, spec: str, line: int, end_line: int, max_lines: int = 120) -> str:
    try:
        lines = read_text(ws.resolve(spec)).splitlines()
    except (PathError, OSError):
        return ""
    if end_line - line + 1 > max_lines:
        return (_numbered(lines, line, line + max_lines - 1)
                + f"\n... ({end_line - line + 1 - max_lines} more lines: read {spec} "
                  f"start={line + max_lines})")
    return _numbered(lines, line, end_line)


def build_pack(ws: Workspace, index: SymbolIndex, changes: SymbolChanges, *, title: str,
               description: str, diff_text: str, bumps: list[pydeps.DependencySource],
               focus_symbols: list[str] | None = None, max_refs_per_symbol: int = 25,
               repo_map_chars: int = 6000) -> EvidencePack:
    pack = EvidencePack()
    pack.add("The merge request", f"Title: {title}\n\nDescription:\n{description or '(none)'}",
             0)
    pack.add("The diff (base...head)", diff_text or "(empty diff)", 1)
    pack.add("What the change does to symbols (from base and head syntax trees)",
             changes.render(), 1)

    bodies: list[str] = []
    for change in changes.changes:
        if change.kind == "variable":
            continue
        head = ""
        if change.line is not None:
            tags = index.cache.tags(ws.resolve(change.path))
            d = tags.definition(change.qualname) if tags else None
            if d is not None:
                head = function_text(ws, change.path, d.line, d.end_line)
        base = ""
        if change.base_line is not None and ws.base is not None:
            base_spec = f"base:{change.path}"
            try:
                tags = index.cache.tags(ws.resolve(base_spec))
            except PathError:
                tags = None
            d = tags.definition(change.qualname) if tags else None
            if d is not None:
                base = function_text(ws, base_spec, d.line, d.end_line)
        part = f"### {change.qualname} ({change.change})\n"
        if base:
            part += f"Before (base:{change.path}):\n{base}\n"
        if head:
            part += f"After ({change.path}):\n{head}\n"
        bodies.append(part)
    pack.add("Changed functions in full, before and after", "\n".join(bodies), 2)

    symbols = focus_symbols if focus_symbols is not None else [
        c.qualname for c in changes.contract_changes if c.kind != "variable"
    ] or [c.qualname for c in changes.changes if c.kind != "variable" and c.change != "added"]
    ref_parts: list[str] = []
    seen_enclosing: set[tuple[str, str]] = set()
    for symbol in symbols[:8]:
        result = index.references(symbol)
        hits = [h for h in result.hits if h.kind not in ("definition", "import")]
        ref_parts.append(result.render(show=max_refs_per_symbol))
        for hit in hits[:max_refs_per_symbol]:
            key = (hit.spec, hit.enclosing)
            if hit.enclosing == "<module>" or key in seen_enclosing:
                continue
            seen_enclosing.add(key)
            try:
                tags = index.cache.tags(ws.resolve(hit.spec))
            except PathError:
                continue
            d = tags.definition(hit.enclosing) if tags else None
            if d is not None:
                ref_parts.append(f"#### {hit.enclosing} in {hit.spec}\n"
                                 + function_text(ws, hit.spec, d.line, d.end_line, 60))
    pack.add("References to the changed symbols, with their enclosing functions",
             "\n\n".join(ref_parts), 3)

    for dep in bumps:
        if dep.bumped and dep.base_path and dep.head_path:
            diff = pydeps.library_diff(dep.base_path, dep.head_path,
                                       f"{dep.name}@{dep.base_version}",
                                       f"{dep.name}@{dep.head_version}")
            pack.add(f"Library diff: {dep.name} {dep.base_version} -> {dep.head_version} "
                     f"(source under dep:{dep.name}@<version>:)", diff, 2)

    focus_files = [f.path for f in ws.change_map().files]
    repo_map = build_repo_map(ws, focus_files, changes.symbols(), cache=index.cache,
                              max_chars=repo_map_chars)
    pack.add("Repository map (ranked around the change)", repo_map, 5)
    roots = ", ".join(label for label in ws.roots(include_base=True))
    pack.add("Roots you can read", f"{roots}\n(bare paths are head; prefix others, e.g. "
             "'base:path', 'dep:<pkg>@<ver>:path', 'repo:<name>:path')", 4)
    return pack


# --------------------------------------------------------------------------- checks


def _norm(text: str) -> str:
    return " ".join(text.split())


def split_spec(path: str, repo: str | None) -> tuple[str | None, str, str]:
    """(root label or None, relative path, revision) for a chain link."""
    revision = "head"
    match = ROOT_LABEL.match(path)
    if match:
        label = match.group(1)
        path = path[match.end():]
        if label == "base":
            return None, path, "base"
        if label == "head":
            label = None
        repo = label or repo
    if repo and not repo.startswith(("dep:", "repo:", "site:")):
        repo = f"repo:{repo}"
    return repo, path.lstrip("/"), revision


@dataclass
class QuoteCheck:
    ok: bool
    link: EvidenceLink | None
    reason: str = ""


def check_quote(ws: Workspace, raw: dict[str, Any] | EvidenceLink) -> QuoteCheck:
    data = raw.model_dump() if isinstance(raw, EvidenceLink) else dict(raw)
    repo, rel, revision = split_spec(str(data.get("path", "")), data.get("repo"))
    if data.get("revision") == "base":
        revision = "base"
    quote = _norm(str(data.get("quote", "")))
    if len(quote.replace(" ", "")) < 3:
        return QuoteCheck(False, None, f"{rel}: no code quoted")
    spec = f"{repo}:{rel}" if repo else (f"base:{rel}" if revision == "base" else rel)
    try:
        lines = read_text(ws.resolve(spec)).splitlines()
    except (PathError, OSError):
        return QuoteCheck(False, None, f"{spec}: no such file")
    try:
        claimed = int(data.get("line") or 0)
    except (TypeError, ValueError):
        claimed = 0
    normalised = [_norm(line) for line in lines]
    hits = [i + 1 for i, line in enumerate(normalised) if quote in line]
    if not hits:  # a multi-line quote: match its first line
        first = _norm(str(data.get("quote", "")).strip().splitlines()[0]) if quote else ""
        hits = [i + 1 for i, line in enumerate(normalised) if first and first in line] \
            if first and first != quote else []
    if not hits:
        return QuoteCheck(False, None, f"{spec}:{claimed}: the quoted code is not in the file")
    line = min(hits, key=lambda n: abs(n - claimed)) if claimed else hits[0]
    if claimed and abs(line - claimed) > 30 and len(hits) > 1:
        return QuoteCheck(False, None, f"{spec}:{claimed}: quote found only far away")
    link = EvidenceLink(path=rel, line=line, quote=lines[line - 1].strip()[:300],
                        claim=str(data.get("claim", ""))[:500],
                        revision="base" if revision == "base" and not repo else "head",
                        repo=repo)
    return QuoteCheck(True, link)


def anchor_from_snippet(ws: Workspace, path: str, snippet: str) -> Anchor | None:
    """The changed line the snippet quotes: an added line (new side) or a removed line
    (old side) in the diff of `path`. None if the snippet is not a changed line."""
    path = path.split(":", 1)[1] if path.startswith(("head:", "base:")) else path
    file = ws.change_map().by_path(path)
    want = _norm(snippet)
    if file is None or len(want.replace(" ", "")) < 3:
        return None
    first = _norm(snippet.strip().splitlines()[0]) if snippet.strip() else want
    for kind, side in (("added", "new"), ("removed", "old")):
        for hunk in file.hunks:
            for line in hunk.lines:
                if line.kind != kind:
                    continue
                text = _norm(line.text)
                if text and (want in text or first in text or (len(text) > 8 and text in want)):
                    number = line.new_lineno if side == "new" else line.old_lineno
                    return Anchor(path=file.path, line=number, side=side,
                                  snippet=line.text.strip())
    return None
