"""Tier 1 code intelligence: tree-sitter tags, any language with a backend.

Per file (cached by git blob SHA, so an unchanged file is parsed once per process):

- definitions: functions, methods, classes, module/class-level names (constants,
  enum members), each with its qualified name, line range and signature;
- references: every identifier use, classified as `call`, `import`, `attribute` or
  `name`, with its enclosing definition.

Lexical, so it over-matches common names; results say `tier=1` and the classification
plus grouping keeps them readable rather than hiding the noise. Precise answers for
Python come from `jedi_backend` (tier 2) behind the same interface.

Languages are a seam: one `Backend` per language. Python is implemented; tree-sitter
already parses C and C++, so a clangd-backed tier 2 and a C tier 1 can slot in later.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from tree_sitter_language_pack import get_parser

# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Definition:
    name: str
    qualname: str
    kind: str  # function | method | class | variable
    line: int  # 1-based, first line (decorators excluded)
    end_line: int
    signature: str  # e.g. "def get_user(uid: int) -> User"
    parent: str | None = None  # enclosing class/function qualname
    bases: tuple[str, ...] = ()  # for classes


@dataclass(frozen=True)
class Reference:
    name: str
    line: int
    col: int
    kind: str  # call | import | attribute | name
    enclosing: str  # qualname of the enclosing definition, or "<module>"
    enclosing_line: int  # first line of the enclosing definition (0 for module)
    receiver: str = ""  # for attribute calls: the text before the dot


@dataclass
class FileTags:
    lang: str
    definitions: list[Definition] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    # per-function facts for the change map
    raises: dict[str, set[str]] = field(default_factory=dict)
    returns_none: dict[str, int] = field(default_factory=dict)
    bodies: dict[str, str] = field(default_factory=dict)  # qualname -> normalised body text
    values: dict[str, str] = field(default_factory=dict)  # variable qualname -> value text

    def definition(self, qualname: str) -> Definition | None:
        for d in self.definitions:
            if d.qualname == qualname:
                return d
        return None

    def enclosing(self, line: int) -> Definition | None:
        best = None
        for d in self.definitions:
            if d.kind in ("function", "method", "class") and d.line <= line <= d.end_line:
                if best is None or d.line >= best.line:
                    best = d
        return best


class Backend(Protocol):
    lang: str
    extensions: tuple[str, ...]

    def extract(self, source: bytes) -> FileTags: ...


# --------------------------------------------------------------------------- python

_PY_SCOPES = {"function_definition", "class_definition"}
_IMPORTS = {"import_statement", "import_from_statement", "future_import_statement"}


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def _normalise(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()
                     and not line.strip().startswith("#"))


class PythonBackend:
    lang = "python"
    extensions = (".py", ".pyi")

    def __init__(self) -> None:
        self._local = threading.local()

    def _parser(self):
        parser = getattr(self._local, "parser", None)
        if parser is None:
            parser = self._local.parser = get_parser("python")
        return parser

    def extract(self, source: bytes) -> FileTags:
        tree = self._parser().parse(source)
        tags = FileTags(lang=self.lang)
        self._walk(tree.root_node, tags, stack=[])
        return tags

    # The walk keeps a stack of enclosing definitions (qualname, kind, first line).
    def _walk(self, node, tags: FileTags, stack: list[tuple[str, str, int]]) -> None:
        kind = node.type
        if kind in _PY_SCOPES:
            self._definition(node, tags, stack)
            return
        if kind in _IMPORTS:
            self._import(node, tags, stack)
            return
        if kind in ("assignment", "augmented_assignment") and stack_is_container(stack):
            self._assignment(node, tags, stack)
        if kind == "raise_statement" and stack:
            exc = node.named_children[0] if node.named_children else None
            if exc is not None:
                target = exc.child_by_field_name("function") if exc.type == "call" else exc
                name = _text(target).split(".")[-1]
                tags.raises.setdefault(stack[-1][0], set()).add(name)
        if kind == "return_statement" and stack:
            value = node.named_children[0] if node.named_children else None
            if value is None or _text(value) == "None":
                tags.returns_none[stack[-1][0]] = tags.returns_none.get(stack[-1][0], 0) + 1
        if kind == "identifier":
            self._identifier(node, tags, stack)
        for child in node.children:
            self._walk(child, tags, stack)

    def _definition(self, node, tags: FileTags, stack) -> None:
        name_node = node.child_by_field_name("name")
        name = _text(name_node)
        parent = stack[-1] if stack else None
        qual = f"{parent[0]}.{name}" if parent else name
        if node.type == "class_definition":
            dkind = "class"
        else:
            dkind = "method" if parent and parent[1] == "class" else "function"
        body = node.child_by_field_name("body")
        header_end = body.start_byte if body is not None else node.end_byte
        signature = node.text[: header_end - node.start_byte].decode("utf-8", "replace")
        signature = " ".join(signature.split()).rstrip(":").strip()
        bases: tuple[str, ...] = ()
        if node.type == "class_definition":
            arglist = node.child_by_field_name("superclasses")
            if arglist is not None:
                bases = tuple(_text(c) for c in arglist.named_children
                              if c.type in ("identifier", "attribute"))
        start = node.start_point[0] + 1
        tags.definitions.append(Definition(
            name=name, qualname=qual, kind=dkind, line=start, end_line=node.end_point[0] + 1,
            signature=signature, parent=parent[0] if parent else None, bases=bases))
        if body is not None:
            tags.bodies[qual] = _normalise(_text(body))
        # Parameters, defaults and annotations are references in the *outer* scope
        # for names (defaults) -- keep it simple: walk them inside the new scope.
        stack.append((qual, dkind if dkind != "method" else "function", start))
        for child in node.children:
            if child is name_node:
                continue
            self._walk(child, tags, stack)
        stack.pop()

    def _assignment(self, node, tags: FileTags, stack) -> None:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or left.type != "identifier":
            return
        parent = stack[-1] if stack else None
        name = _text(left)
        qual = f"{parent[0]}.{name}" if parent else name
        if not any(d.qualname == qual for d in tags.definitions):
            tags.definitions.append(Definition(
                name=name, qualname=qual, kind="variable", line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1, signature=" ".join(_text(node).split())[:160],
                parent=parent[0] if parent else None))
        tags.values[qual] = " ".join(_text(right).split()) if right is not None else ""

    def _import(self, node, tags: FileTags, stack) -> None:
        enclosing, line0 = (stack[-1][0], stack[-1][2]) if stack else ("<module>", 0)
        module = node.child_by_field_name("module_name")
        for ident in _identifiers(node):
            if module is not None and _inside(ident, module):
                continue  # `from a.b import c`: c is the imported name; a.b is the module
            tags.references.append(Reference(
                name=_text(ident), line=ident.start_point[0] + 1, col=ident.start_point[1] + 1,
                kind="import", enclosing=enclosing, enclosing_line=line0))
        if module is not None:
            last = _identifiers(module)
            if last:
                ident = last[-1]
                tags.references.append(Reference(
                    name=_text(ident), line=ident.start_point[0] + 1,
                    col=ident.start_point[1] + 1, kind="import", enclosing=enclosing,
                    enclosing_line=line0))

    def _identifier(self, node, tags: FileTags, stack) -> None:
        parent = node.parent
        if parent is None:
            return
        ptype = parent.type
        if ptype in _PY_SCOPES and parent.child_by_field_name("name") == node:
            return
        if ptype == "keyword_argument" and parent.child_by_field_name("name") == node:
            return
        if ptype in ("parameters", "lambda_parameters"):
            return  # a bare parameter name
        if ptype in ("assignment", "augmented_assignment") and \
                parent.child_by_field_name("left") == node:
            return  # a binding, not a use
        if ptype in ("default_parameter", "typed_parameter",
                     "typed_default_parameter") and node == _param_name(parent):
            return
        receiver = ""
        if ptype == "call" and parent.child_by_field_name("function") == node:
            kind = "call"
        elif ptype == "attribute" and parent.child_by_field_name("attribute") == node:
            grand = parent.parent
            receiver = _text(parent.child_by_field_name("object"))[:80]
            if grand is not None and grand.type == "call" and \
                    grand.child_by_field_name("function") == parent:
                kind = "call"
            else:
                kind = "attribute"
        else:
            kind = "name"
        enclosing, line0 = (stack[-1][0], stack[-1][2]) if stack else ("<module>", 0)
        tags.references.append(Reference(
            name=_text(node), line=node.start_point[0] + 1, col=node.start_point[1] + 1,
            kind=kind, enclosing=enclosing, enclosing_line=line0, receiver=receiver))


def stack_is_container(stack) -> bool:
    """Module- and class-level assignments define names; function locals do not."""
    return not stack or stack[-1][1] == "class"


def _identifiers(node) -> list:
    out = []
    pending = [node]
    while pending:
        current = pending.pop()
        if current.type == "identifier":
            out.append(current)
        pending.extend(reversed(current.children))
    return out


def _inside(node, container) -> bool:
    return container.start_byte <= node.start_byte and node.end_byte <= container.end_byte


def _param_name(param):
    if param.type == "parameters":
        return None
    name = param.child_by_field_name("name")
    if name is not None:
        return name
    for child in param.children:
        if child.type == "identifier":
            return child
    return None


# --------------------------------------------------------------------------- index

BACKENDS: dict[str, Backend] = {}


def register(backend: Backend) -> None:
    for ext in backend.extensions:
        BACKENDS[ext] = backend


register(PythonBackend())


def backend_for(path: Path | str) -> Backend | None:
    return BACKENDS.get(Path(path).suffix)


def blob_sha(data: bytes) -> str:
    """Git's blob id for `data`, so caches line up with git objects."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class TagCache:
    """FileTags by blob SHA. Thread-safe; shared across a job."""

    def __init__(self, max_entries: int = 50_000) -> None:
        self._entries: dict[str, FileTags] = {}
        self._lock = threading.Lock()
        self.max_entries = max_entries
        self.parsed = 0

    def tags_for_bytes(self, data: bytes, suffix: str) -> FileTags | None:
        backend = BACKENDS.get(suffix)
        if backend is None:
            return None
        key = f"{backend.lang}:{blob_sha(data)}"
        with self._lock:
            hit = self._entries.get(key)
        if hit is not None:
            return hit
        tags = backend.extract(data)
        with self._lock:
            if len(self._entries) >= self.max_entries:
                self._entries.clear()
            self._entries[key] = tags
            self.parsed += 1
        return tags

    def tags(self, path: Path) -> FileTags | None:
        if backend_for(path) is None or not path.is_file():
            return None
        return self.tags_for_bytes(path.read_bytes(), path.suffix)
