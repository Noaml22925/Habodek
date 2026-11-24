"""Definitions, references and callers, across every root of the workspace.

Tier 1: ripgrep finds candidate files for the name, tree-sitter classifies each hit
(call, import, attribute, name) and names its enclosing definition. Tier 2 (Python):
jedi, from the definition, gives the precise set; each hit is still classified by
tier 1. Every result says which tier answered, and falls back to tier 1 rather than
failing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from habodek.brain.codeintel.files import SKIP_DIRS, read_text
from habodek.brain.codeintel.ts_index import Definition, TagCache, backend_for
from habodek.brain.codeintel.workspace import PathError, Workspace

SHOW = 80


@dataclass(frozen=True)
class DefHit:
    spec: str
    definition: Definition

    def render(self) -> str:
        d = self.definition
        return f"{self.spec}:{d.line}-{d.end_line} {d.kind} {d.qualname}: {d.signature}"


@dataclass(frozen=True)
class RefHit:
    spec: str
    line: int
    col: int
    kind: str  # call | import | attribute | name | definition | text
    enclosing: str
    code: str
    confirmed: bool = True  # False: a lexical (tier 1) match tier 2 could not resolve

    @property
    def package(self) -> str:
        if ":" in self.spec.split("/")[0]:
            label, _, rel = self.spec.partition(":")
            if label in ("dep", "repo", "site"):  # dep:name@v:rel
                more, _, rel = rel.partition(":")
                label = f"{label}:{more}"
            top = rel.split("/")[0] if "/" in rel else ""
            return f"{label}/{top}" if top else label
        return self.spec.split("/")[0] if "/" in self.spec else "(root)"


@dataclass
class RefResult:
    name: str
    tier: int
    hits: list[RefHit] = field(default_factory=list)
    note: str = ""

    def by_kind(self, *kinds: str) -> list[RefHit]:
        return [h for h in self.hits if h.kind in kinds]

    def render(self, show: int = SHOW) -> str:
        uses = [h for h in self.hits if h.kind != "definition"]
        files = {h.spec for h in uses}
        packages = {h.package for h in uses}
        counts = defaultdict(int)
        for h in uses:
            counts[h.kind] += 1
        summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
        lines = [f"references to {self.name!r} (tier {self.tier}): {len(uses)} in {len(files)} "
                 f"files, {len(packages)} packages ({summary or 'none'})"]
        if self.note:
            lines.append(f"note: {self.note}")
        grouped: dict[str, list[RefHit]] = defaultdict(list)
        for h in self.hits:
            grouped[h.package].append(h)
        shown = 0
        for package in sorted(grouped):
            lines.append(f"[{package}]")
            for h in sorted(grouped[package], key=lambda h: (h.spec, h.line)):
                if shown >= show:
                    break
                where = f" in {h.enclosing}" if h.enclosing != "<module>" else ""
                mark = "" if h.confirmed else "?"
                lines.append(f"  {h.spec}:{h.line} [{h.kind}{mark}]{where}: {h.code}")
                shown += 1
        if shown < len(self.hits):
            lines.append(f"... {len(self.hits) - shown} more; filter with kinds or path_glob")
        return "\n".join(lines)


class SymbolIndex:
    """Definitions and references over a workspace, with a shared tag cache."""

    def __init__(self, ws: Workspace, cache: TagCache | None = None,
                 use_jedi: bool = True) -> None:
        self.ws = ws
        self.cache = cache or TagCache()
        self.use_jedi = use_jedi
        self._jedi = None
        self._jedi_lock = threading.Lock()  # jedi is not thread-safe; agents run in parallel

    # ----------------------------------------------------------------- helpers
    def _roots(self, roots: list[str] | None) -> list[tuple[str, Path]]:
        labels = roots or list(self.ws.roots())
        out = []
        for label in labels:
            try:
                out.append((label, self.ws.root(label)))
            except PathError:
                continue
        return out

    def candidate_files(self, name: str, roots: list[str] | None = None,
                        path_glob: str | None = None) -> list[Path]:
        rg = shutil.which("rg")
        files: list[Path] = []
        for _, root in self._roots(roots):
            if rg:
                cmd = [rg, "-l", "-w", "-F", "--no-config", "-e", name]
                for skip in SKIP_DIRS:
                    cmd += ["--glob", f"!{skip}"]
                if path_glob:
                    cmd += ["--glob", path_glob]
                proc = subprocess.run(cmd + [str(root)], capture_output=True, timeout=120)
                files += [Path(p) for p in proc.stdout.decode("utf-8", "replace").splitlines()
                          if p]
            else:  # pragma: no cover - rg is in every image
                pattern = re.compile(rf"\b{re.escape(name)}\b")
                files += [p for p in root.rglob("*") if p.is_file() and
                          pattern.search(read_text(p))]
        return sorted(set(files))

    def _line(self, path: Path, line: int) -> str:
        try:
            lines = read_text(path).splitlines()
            return lines[line - 1].strip()[:200] if 0 < line <= len(lines) else ""
        except OSError:
            return ""

    # ----------------------------------------------------------------- definitions
    def definitions(self, name: str, roots: list[str] | None = None) -> list[DefHit]:
        short = name.split(".")[-1]
        hits: list[DefHit] = []
        for path in self.candidate_files(short, roots):
            tags = self.cache.tags(path)
            if tags is None:
                continue
            for d in tags.definitions:
                if d.name == name or d.qualname == name or (
                        "." in name and d.qualname.endswith("." + name)) or (
                        "." not in name and d.name == short):
                    hits.append(DefHit(self.ws.display(path), d))
        hits.sort(key=lambda h: (h.spec.count(":"), h.spec, h.definition.line))
        return hits

    # ----------------------------------------------------------------- references
    def references(self, name: str, roots: list[str] | None = None,
                   kinds: list[str] | None = None, path_glob: str | None = None,
                   tier: str = "auto", definition: DefHit | None = None) -> RefResult:
        short = name.split(".")[-1]
        result: RefResult | None = None
        if tier in ("auto", "2") and self.use_jedi:
            target = definition
            if target is None:
                defs = [d for d in self.definitions(name, roots)
                        if backend_for(d.spec.split(":")[-1]) is not None and
                        d.spec.endswith((".py", ".pyi"))]
                if len(defs) == 1:
                    target = defs[0]
                elif len(defs) > 1:
                    note = (f"{len(defs)} definitions named {name!r}; tier 1 used. Pass a "
                            "qualified name (Class.method) to pick one.")
                    result = self._tier1(short, roots, path_glob)
                    result.note = note
            if target is not None and result is None:
                result = self._tier2(short, target)
        if result is None:
            result = self._tier1(short, roots, path_glob)
        if kinds:
            result.hits = [h for h in result.hits if h.kind in kinds]
        return result

    def _tier1(self, name: str, roots: list[str] | None, path_glob: str | None) -> RefResult:
        hits: list[RefHit] = []
        for path in self.candidate_files(name, roots, path_glob):
            spec = self.ws.display(path)
            tags = self.cache.tags(path)
            if tags is None:  # a language without a backend: plain text hits
                for number, text in enumerate(read_text(path).splitlines(), 1):
                    if re.search(rf"\b{re.escape(name)}\b", text):
                        hits.append(RefHit(spec, number, 1, "text", "<module>",
                                           text.strip()[:200]))
                continue
            for d in tags.definitions:
                if d.name == name:
                    hits.append(RefHit(spec, d.line, 1, "definition", d.parent or "<module>",
                                       self._line(path, d.line)))
            for r in tags.references:
                if r.name == name:
                    hits.append(RefHit(spec, r.line, r.col, r.kind, r.enclosing,
                                       self._line(path, r.line)))
        return RefResult(name, 1, hits)

    def _jedi_backend(self):
        if self._jedi is None:
            from habodek.brain.codeintel.jedi_backend import JediBackend

            extra = [root for label, root in self.ws.roots().items() if label != "head"]
            self._jedi = JediBackend(self.ws.head, extra)
        return self._jedi

    def _tier2(self, name: str, target: DefHit) -> RefResult:
        try:
            path = self.ws.resolve(target.spec)
            text = read_text(path).splitlines()[target.definition.line - 1]
            col = _name_column(text, name)
            with self._jedi_lock:
                refs = self._jedi_backend().references(path, target.definition.line, col)
        except Exception as exc:  # noqa: BLE001 - tier 1 is always there
            result = self._tier1(name, None, None)
            result.note = f"tier 2 failed ({type(exc).__name__}: {exc}); tier 1 used"
            return result
        hits: list[RefHit] = []
        for ref in refs:
            spec = self.ws.display(ref.path)
            if ref.path.is_absolute() and spec == str(ref.path):
                continue  # outside the workspace (stdlib)
            kind, enclosing = "definition" if ref.is_definition else "name", "<module>"
            tags = self.cache.tags(ref.path)
            if tags is not None:
                for r in tags.references:
                    if r.line == ref.line and r.col == ref.col and r.name == name:
                        kind, enclosing = r.kind, r.enclosing
                        break
                else:
                    outer = tags.enclosing(ref.line)
                    if outer is not None and not ref.is_definition:
                        enclosing = outer.qualname
            hits.append(RefHit(spec, ref.line, ref.col, kind, enclosing,
                               self._line(ref.path, ref.line)))
        # jedi is precise but incomplete where it cannot infer a receiver's type
        # (`obj.method()` with `obj` untyped). Missing a caller is the worst failure
        # for a reviewer, so lexical matches it did not resolve are kept, marked.
        seen = {(h.spec, h.line) for h in hits}
        extra = [RefHit(h.spec, h.line, h.col, h.kind, h.enclosing, h.code, confirmed=False)
                 for h in self._tier1(name, None, None).hits
                 if (h.spec, h.line) not in seen and h.kind != "definition"]
        result = RefResult(name, 2, hits + extra)
        result.note = f"from the definition at {target.spec}:{target.definition.line}"
        if extra:
            result.note += (f"; {len(extra)} lexical matches tier 2 could not resolve are "
                            "marked '?' -- check their receiver")
        return result

    # ----------------------------------------------------------------- outline
    def outline(self, spec: str) -> str:
        path = self.ws.resolve(spec)
        shown = self.ws.display(path)
        tags = self.cache.tags(path)
        if tags is None:
            return f"outline {shown}: no symbol backend for this file type; use read or grep"
        lines = [f"outline {shown} ({len(read_text(path).splitlines())} lines)"]
        for d in sorted(tags.definitions, key=lambda d: d.line):
            depth = d.qualname.count(".")
            span = f"{d.line}-{d.end_line}" if d.end_line != d.line else f"{d.line}"
            lines.append(f"{span:>9}  {'  ' * depth}{d.signature}")
        return "\n".join(lines)

    # ----------------------------------------------------------------- callers
    def callers(self, name: str, depth: int = 2, roots: list[str] | None = None,
                max_nodes: int = 60) -> str:
        lines = [f"callers of {name!r} (depth {depth}, tier 1 + tier 2 where available)"]
        seen: set[tuple[str, str]] = set()
        count = 0

        def walk(target: str, level: int) -> None:
            nonlocal count
            refs = self.references(target, roots, kinds=["call"])
            groups: dict[tuple[str, str], list[RefHit]] = defaultdict(list)
            for hit in refs.hits:
                groups[(hit.spec, hit.enclosing)].append(hit)
            for (spec, enclosing), hits in sorted(groups.items()):
                if count >= max_nodes:
                    lines.append("  " * level + "... (limit reached)")
                    return
                count += 1
                at = ",".join(str(h.line) for h in hits[:5])
                lines.append("  " * level + f"- {enclosing} ({spec}:{at}): {hits[0].code}")
                key = (spec, enclosing)
                if enclosing != "<module>" and level < depth and key not in seen:
                    seen.add(key)
                    walk(enclosing.split(".")[-1], level + 1)

        walk(name, 1)
        if len(lines) == 1:
            lines.append("  (no calls found)")
        return "\n".join(lines)


def _name_column(text: str, name: str) -> int:
    match = re.search(rf"\b(?:def|class)\s+({re.escape(name)})\b", text) or \
        re.search(rf"\b({re.escape(name)})\b", text)
    return (match.start(1) + 1) if match else 1
