"""A ranked map of the repository, personalised to the change.

The approach is Aider's repo map (github.com/Aider-AI/aider, `aider/repomap.py`,
Apache-2.0): files are nodes, an edge runs from a file that references an identifier
to the file that defines it, PageRank is personalised toward the files and
identifiers the change touches, and the top-ranked definitions are listed with their
signatures within a size budget. This is a re-implementation on our tag index, not a
copy of Aider's code; the credit is recorded in NOTICE.

It gives an agent orientation ("what around this change matters") in a few thousand
tokens, before it starts reading.
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

from habodek.brain.codeintel.files import SKIP_DIRS
from habodek.brain.codeintel.ts_index import TagCache, backend_for
from habodek.brain.codeintel.workspace import Workspace

# Identifiers that say nothing about structure.
_COMMON = {"self", "cls", "None", "True", "False", "str", "int", "dict", "list", "len",
           "print", "isinstance", "super", "range", "object", "Exception", "get", "set"}


def pagerank(nodes: list[str], edges: dict[tuple[str, str], float],
             personal: dict[str, float] | None = None, damping: float = 0.85,
             iterations: int = 60) -> dict[str, float]:
    if not nodes:
        return {}
    index = {n: i for i, n in enumerate(nodes)}
    out_weight: dict[str, float] = defaultdict(float)
    incoming: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (src, dst), w in edges.items():
        out_weight[src] += w
        incoming[dst].append((src, w))
    total_personal = sum((personal or {}).values())
    if total_personal > 0:
        teleport = {n: (personal or {}).get(n, 0.0) / total_personal for n in nodes}
    else:
        teleport = {n: 1.0 / len(nodes) for n in nodes}
    rank = dict(teleport)
    for _ in range(iterations):
        dangling = sum(rank[n] for n in nodes if out_weight[n] == 0)
        new = {}
        for n in nodes:
            flow = sum(rank[src] * w / out_weight[src] for src, w in incoming[n])
            new[n] = (1 - damping) * teleport[n] + damping * (flow + dangling * teleport[n])
        rank = new
    del index
    return rank


def build_repo_map(ws: Workspace, focus_files: list[str], focus_idents: list[str],
                   cache: TagCache | None = None, max_chars: int = 12_000,
                   max_files: int = 4000) -> str:
    cache = cache or TagCache()
    root = ws.head.resolve()
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if path.is_file() and backend_for(path):
            files.append(path)
        if len(files) >= max_files:
            break
    specs = {p: ws.display(p) for p in files}
    defines: dict[str, set[str]] = defaultdict(set)
    references: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    definitions: dict[tuple[str, str], list] = defaultdict(list)
    for path in files:
        tags = cache.tags(path)
        if tags is None:
            continue
        spec = specs[path]
        for d in tags.definitions:
            if d.kind != "variable" or d.parent is None:
                defines[d.name].add(spec)
                definitions[(spec, d.name)].append(d)
        for r in tags.references:
            if r.name not in _COMMON:
                references[spec][r.name] += 1
    focus_set = set(focus_idents)
    edges: dict[tuple[str, str], float] = defaultdict(float)
    ident_edges: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for src, counts in references.items():
        for ident, count in counts.items():
            targets = defines.get(ident)
            if not targets:
                continue
            weight = math.sqrt(count)
            if ident in focus_set:
                weight *= 10
            if ident.startswith("_"):
                weight *= 0.1
            if len(targets) > 5:  # defined everywhere: not structural
                weight *= 0.1
            for dst in targets:
                if dst == src:
                    continue
                edges[(src, dst)] += weight
                ident_edges[(src, dst)][ident] = ident_edges[(src, dst)].get(ident, 0) + weight
    nodes = sorted(set(specs.values()))
    personal = {spec: 1.0 for spec in nodes}
    for spec in focus_files:
        if spec in personal:
            personal[spec] = 100.0
    rank = pagerank(nodes, edges, personal)
    # Distribute each file's rank over the definitions it points to.
    def_rank: dict[tuple[str, str], float] = defaultdict(float)
    out_total: dict[str, float] = defaultdict(float)
    for (src, _), w in edges.items():
        out_total[src] += w
    for (src, dst), idents in ident_edges.items():
        for ident, w in idents.items():
            def_rank[(dst, ident)] += rank.get(src, 0) * w / (out_total[src] or 1)
    for spec in focus_files:  # the changed files' own definitions come first
        for (dspec, ident) in list(definitions):
            if dspec == spec:
                def_rank[(dspec, ident)] += 10.0
    ranked = sorted(def_rank.items(), key=lambda kv: -kv[1])
    by_file: dict[str, list] = defaultdict(list)
    order: list[str] = []
    for (spec, ident), _score in ranked:
        for d in definitions.get((spec, ident), []):
            if spec not in by_file:
                order.append(spec)
            if d not in by_file[spec]:
                by_file[spec].append(d)
    lines: list[str] = []
    size = 0
    for spec in order:
        block = [f"{spec}:"] + [f"  {d.line:>5} {d.signature}" for d in
                                sorted(by_file[spec], key=lambda d: d.line)]
        block_size = sum(len(b) + 1 for b in block)
        if size + block_size > max_chars:
            if not lines:
                lines.extend(block[: max(2, max_chars // 80)])
            break
        lines.extend(block)
        size += block_size
    return "\n".join(lines) if lines else "(no definitions found)"
