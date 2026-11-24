"""What the change does to the code's symbols, from base and head syntax trees.

Not read from the diff: both versions of each changed file are parsed and their
definitions compared. Per symbol: added, removed, modified (body changed), signature
changed; for functions also the mechanically visible contract changes — exceptions
no longer raised or newly raised, `None` returns added. Module and class-level
names (constants, enum members, `__all__`) report value changes.

This seeds the evidence packs and the finders: "get_user no longer raises NotFound
and now returns None" is the contract change a reviewer must chase to every caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from habodek.brain.codeintel.ts_index import FileTags, TagCache, backend_for
from habodek.brain.codeintel.workspace import Workspace
from habodek.workspace.gitcmd import GitError, run_git


@dataclass
class SymbolChange:
    path: str
    qualname: str
    kind: str  # function | method | class | variable
    change: str  # added | removed | modified
    line: int | None  # at head (None when removed)
    base_line: int | None
    signature: str = ""
    old_signature: str = ""
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else f"{self.path} (base:{self.base_line})"
        text = f"- {self.change} {self.kind} {self.qualname} at {where}"
        if self.old_signature and self.old_signature != self.signature:
            text += f"\n    signature: {self.old_signature}  ->  {self.signature}"
        for note in self.notes:
            text += f"\n    {note}"
        return text


@dataclass
class SymbolChanges:
    changes: list[SymbolChange]
    unparsed: list[str]  # changed files without a language backend

    def for_path(self, path: str) -> list[SymbolChange]:
        return [c for c in self.changes if c.path == path]

    @property
    def contract_changes(self) -> list[SymbolChange]:
        """Changes visible to callers: removals, signatures, raises/returns, values."""
        return [c for c in self.changes if c.change == "removed" or c.notes or
                (c.old_signature and c.old_signature != c.signature)]

    def symbols(self) -> list[str]:
        return sorted({c.qualname.split(".")[-1] for c in self.changes})

    def render(self) -> str:
        if not self.changes and not self.unparsed:
            return "no symbol-level changes"
        lines = [c.render() for c in self.changes]
        if self.unparsed:
            lines.append("changed files without symbol analysis: " + ", ".join(self.unparsed))
        return "\n".join(lines)


def _base_bytes(ws: Workspace, path: str) -> bytes | None:
    if ws.base is not None:
        candidate = ws.base / path
        return candidate.read_bytes() if candidate.is_file() else None
    if ws.repo is None:
        return None
    try:
        return run_git(ws.repo, "show", f"{ws.base_sha}:{path}").encode("utf-8")
    except GitError:
        return None


def compare(path: str, old: FileTags | None, new: FileTags | None) -> list[SymbolChange]:
    old_defs = {d.qualname: d for d in (old.definitions if old else [])}
    new_defs = {d.qualname: d for d in (new.definitions if new else [])}
    out: list[SymbolChange] = []
    for qual, d in new_defs.items():
        before = old_defs.get(qual)
        if before is None:
            out.append(SymbolChange(path, qual, d.kind, "added", d.line, None, d.signature))
            continue
        notes: list[str] = []
        if d.kind == "variable":
            old_value = old.values.get(qual, "") if old else ""
            new_value = new.values.get(qual, "") if new else ""
            if old_value != new_value:
                notes.append(f"value: {old_value[:80]}  ->  {new_value[:80]}")
        else:
            old_raises = old.raises.get(qual, set()) if old else set()
            new_raises = new.raises.get(qual, set()) if new else set()
            for name in sorted(old_raises - new_raises):
                notes.append(f"no longer raises {name}")
            for name in sorted(new_raises - old_raises):
                notes.append(f"now raises {name}")
            old_none = old.returns_none.get(qual, 0) if old else 0
            new_none = new.returns_none.get(qual, 0) if new else 0
            if new_none > old_none:
                notes.append(f"returns None on {new_none - old_none} more path(s)"
                             + (" (it never did before)" if old_none == 0 else ""))
            if d.bases != before.bases:
                notes.append(f"bases: {', '.join(before.bases) or '-'}  ->  "
                             f"{', '.join(d.bases) or '-'}")
        body_changed = (old.bodies.get(qual) if old else None) != \
            (new.bodies.get(qual) if new else None)
        if body_changed or notes or d.signature != before.signature:
            out.append(SymbolChange(path, qual, d.kind, "modified", d.line, before.line,
                                    d.signature, before.signature, notes))
    for qual, d in old_defs.items():
        if qual not in new_defs:
            out.append(SymbolChange(path, qual, d.kind, "removed", None, d.line, "",
                                    d.signature))
    # A class whose methods all changed is noise at class level: keep the class
    # entry only if something about the class itself changed.
    method_parents = {c.qualname.rsplit(".", 1)[0] for c in out if "." in c.qualname}
    return [c for c in out if not (c.kind == "class" and c.change == "modified" and not c.notes
                                   and c.signature == c.old_signature
                                   and c.qualname in method_parents)]


def symbol_changes(ws: Workspace, cache: TagCache | None = None) -> SymbolChanges:
    cache = cache or TagCache()
    changes: list[SymbolChange] = []
    unparsed: list[str] = []
    for file in ws.change_map().files:
        path = file.path
        if backend_for(path) is None:
            unparsed.append(path)
            continue
        suffix = "." + path.rsplit(".", 1)[-1]
        new_tags = None
        if file.status != "deleted":
            head_file = ws.head / file.new_path
            if head_file.is_file():
                new_tags = cache.tags_for_bytes(head_file.read_bytes(), suffix)
        old_tags = None
        if file.status != "added":
            data = _base_bytes(ws, file.old_path)
            if data is not None:
                old_tags = cache.tags_for_bytes(data, suffix)
        changes += compare(path, old_tags, new_tags)
    return SymbolChanges(changes, unparsed)
