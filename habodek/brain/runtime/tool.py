"""Tools: what an agent can do, described to the model as JSON-schema functions.

A `Tool` has a name, a description written for the model, a parameter schema, and a
`run(args, ctx) -> str`. A *terminal* tool ends the run: its arguments are the run's
structured output (a hypothesis list, a conclusion, a verdict), validated by schema
and optionally by a semantic check that returns an error message for the model.

`code_tools()` builds the exploration toolset over a workspace. Descriptions ask for
batching ("several paths per read; independent calls in one turn") because requests,
not tokens, are the scarce resource.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from habodek.brain.codeintel import files
from habodek.brain.codeintel.refs import SymbolIndex
from habodek.brain.codeintel.workspace import PathError, Workspace
from habodek.brain.llm.shaping import ToolSpec

MAX_TOOL_OUTPUT = 60_000  # characters of one tool result put into history


@dataclass
class ToolContext:
    ws: Workspace
    index: SymbolIndex
    notes: list[str] = field(default_factory=list)
    files_read: set[str] = field(default_factory=set)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[dict[str, Any], ToolContext], str] | None = None
    terminal: bool = False
    # For terminal tools: a semantic check beyond the schema; returns an error or None.
    check: Callable[[dict[str, Any], ToolContext], str | None] | None = None
    reads: bool = False  # counts as progress (a new read) for the loop guards

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_ROOTS = {"type": "array", "items": {"type": "string"},
          "description": "Roots to search: 'head' (default), 'base', 'dep:<pkg>@<version>', "
                         "'repo:<name>', 'site:<n>'. Omit for every root except base."}


def _cap(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT:
        return text
    return text[:MAX_TOOL_OUTPUT] + (f"\n... output cut at {MAX_TOOL_OUTPUT} characters; "
                                     "ask for a narrower range or pattern")


# --------------------------------------------------------------------------- code tools


def _read(args: dict[str, Any], ctx: ToolContext) -> str:
    paths = args["paths"]
    for spec in paths:
        ctx.files_read.add(spec)
    return files.read_many(ctx.ws, paths, args.get("start", 1), args.get("end"))


def _grep(args: dict[str, Any], ctx: ToolContext) -> str:
    return files.grep(ctx.ws, args["pattern"], roots=args.get("roots"),
                      path_glob=args.get("path_glob"), ignore_case=args.get("ignore_case", False),
                      fixed=args.get("fixed", False), word=args.get("word", False)).render()


def _glob(args: dict[str, Any], ctx: ToolContext) -> str:
    return files.glob(ctx.ws, args["pattern"], root=args.get("root", "head"))


def _outline(args: dict[str, Any], ctx: ToolContext) -> str:
    return "\n\n".join(ctx.index.outline(p) for p in args["paths"])


def _definition(args: dict[str, Any], ctx: ToolContext) -> str:
    hits = ctx.index.definitions(args["name"], args.get("roots"))
    if not hits:
        return f"no definition of {args['name']!r} found (tier 1). Try grep."
    return "\n".join(h.render() for h in hits[:30])


def _references(args: dict[str, Any], ctx: ToolContext) -> str:
    return ctx.index.references(args["name"], args.get("roots"), kinds=args.get("kinds"),
                                path_glob=args.get("path_glob")).render()


def _callers(args: dict[str, Any], ctx: ToolContext) -> str:
    return ctx.index.callers(args["name"], depth=min(int(args.get("depth", 2)), 4),
                             roots=args.get("roots"))


def _git_log(args: dict[str, Any], ctx: ToolContext) -> str:
    if args.get("start") and args.get("path"):
        return ctx.ws.git_log_lines(args["path"], args["start"], args.get("end", args["start"]))
    return ctx.ws.git_log(args.get("path"), max_count=min(int(args.get("max_count", 20)), 50))


def _git_blame(args: dict[str, Any], ctx: ToolContext) -> str:
    return ctx.ws.git_blame(args["path"], args["start"], args.get("end", args["start"]))


def _git_show(args: dict[str, Any], ctx: ToolContext) -> str:
    return ctx.ws.git_show(args["rev"], args.get("path"))


def _note(args: dict[str, Any], ctx: ToolContext) -> str:
    ctx.notes.append(args["text"].strip())
    return f"noted ({len(ctx.notes)} notes). Notes survive context compaction."


def code_tools(include_git: bool = True) -> list[Tool]:
    tools = [
        Tool("read", "Read files with line numbers. Pass every file you need in one call "
             "(several paths). Whole files up to 2,000 lines; for longer ones pass start/end. "
             "Paths: 'api/x.py' (head), 'base:api/x.py' (before the change), "
             "'dep:<pkg>@<ver>:path', 'site:<n>:path'. A directory lists its entries.",
             _obj({"paths": {"type": "array", "items": _STR, "minItems": 1},
                   "start": _INT, "end": _INT}, ["paths"]), _read, reads=True),
        Tool("grep", "Search file contents with a regular expression (ripgrep). Reports total "
             "matches; narrow with path_glob or word=true when told to.",
             _obj({"pattern": _STR, "roots": _ROOTS, "path_glob": _STR,
                   "ignore_case": {"type": "boolean"}, "fixed": {"type": "boolean"},
                   "word": {"type": "boolean"}}, ["pattern"]), _grep, reads=True),
        Tool("glob", "List files matching a glob pattern, e.g. '**/test_*.py'.",
             _obj({"pattern": _STR, "root": _STR}, ["pattern"]), _glob),
        Tool("outline", "Classes and functions of files, with line ranges and signatures.",
             _obj({"paths": {"type": "array", "items": _STR, "minItems": 1}}, ["paths"]),
             _outline, reads=True),
        Tool("definition", "Where a function, class, method ('Class.method') or constant is "
             "defined, across the repository and its dependencies.",
             _obj({"name": _STR, "roots": _ROOTS}, ["name"]), _definition, reads=True),
        Tool("references", "Every use of a symbol, classified (call, import, attribute, name) "
             "with the enclosing function, grouped by package, across the repository and its "
             "dependencies. Use 'Class.method' for a method. Hits marked '?' are textual "
             "matches whose receiver type could not be resolved: check them.",
             _obj({"name": _STR, "roots": _ROOTS, "path_glob": _STR,
                   "kinds": {"type": "array", "items": {"type": "string", "enum": [
                       "call", "import", "attribute", "name", "definition", "text"]}}},
                  ["name"]), _references, reads=True),
        Tool("callers", "The caller tree of a function: who calls it, who calls those, to "
             "the given depth (default 2, max 4).",
             _obj({"name": _STR, "depth": _INT, "roots": _ROOTS}, ["name"]), _callers,
             reads=True),
        Tool("note", "Record an established fact, with file:line evidence, or an open "
             "question. Notes are kept when older context is compacted.",
             _obj({"text": {"type": "string", "minLength": 3}}, ["text"]), _note),
    ]
    if include_git:
        tools += [
            Tool("git_log", "Commit history (newest first) of the repository, of a path, or "
                 "of a line range (path + start/end).",
                 _obj({"path": _STR, "start": _INT, "end": _INT, "max_count": _INT}, []),
                 _git_log, reads=True),
            Tool("git_blame", "Who last changed each line of a range, and in which commit.",
                 _obj({"path": _STR, "start": _INT, "end": _INT}, ["path", "start"]),
                 _git_blame, reads=True),
            Tool("git_show", "A commit (message and patch), or a file at a revision when "
                 "path is given.", _obj({"rev": _STR, "path": _STR}, ["rev"]), _git_show,
                 reads=True),
        ]
    return tools


def run_tool(tool: Tool, args: dict[str, Any], ctx: ToolContext) -> str:
    if tool.run is None:
        return f"error: {tool.name} cannot be run"
    try:
        return _cap(tool.run(args, ctx))
    except PathError as exc:
        return f"error: {exc}"
    except Exception as exc:  # noqa: BLE001 - a tool error is information for the model
        return f"error: {tool.name} failed: {type(exc).__name__}: {str(exc)[:500]}"
