"""`recall(query)`: BM25 over the memory repository (design §6.8).

No embedding model is needed offline. The corpus is the project's own memory (card,
lessons, candidates included) plus the other projects' cards, split into sections
and paragraphs. A hit names its file, so an agent can say where it learned something
— and is told that memory is context to check against the code, never evidence.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from habodek.brain.memory.store import MemoryView, project_dir
from habodek.brain.runtime.tool import Tool, ToolContext

_TERM = re.compile(r"[a-z0-9_]+")
CHUNK = 1200


def terms(text: str) -> list[str]:
    out = []
    for word in _TERM.findall(text.lower()):
        out.append(word)
        if "_" in word:  # snake_case parts too
            out += [p for p in word.split("_") if p]
    return out


@dataclass
class Chunk:
    path: str
    heading: str
    text: str


def chunks(path: str, text: str) -> list[Chunk]:
    out: list[Chunk] = []
    heading = ""
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        while body:
            out.append(Chunk(path, heading, body[:CHUNK]))
            body = body[CHUNK:].strip()
        buf.clear()

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            heading = line.lstrip("#").strip()
        elif not line.strip() and sum(len(b) for b in buf) > CHUNK // 2:
            flush()
        else:
            buf.append(line)
    flush()
    return out


class BM25:
    def __init__(self, docs: list[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = docs
        self.k1, self.b = k1, b
        self.tf = [Counter(terms(d.heading + " " + d.text)) for d in docs]
        self.len = [sum(tf.values()) for tf in self.tf]
        self.avg = sum(self.len) / len(self.len) if self.len else 0.0
        df: Counter[str] = Counter()
        for tf in self.tf:
            df.update(tf.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, limit: int = 5) -> list[tuple[float, Chunk]]:
        q = set(terms(query))
        scored = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    norm = 1 - self.b + self.b * self.len[i] / (self.avg or 1)
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * norm)
            if s > 0:
                scored.append((s, self.docs[i]))
        scored.sort(key=lambda x: -x[0])
        return scored[:limit]


def corpus(view: MemoryView) -> list[Chunk]:
    docs: list[Chunk] = []
    own = project_dir(view.project) + "/"
    for path in view.files():
        if not path.endswith(".md"):
            continue
        if path.startswith(own) or path.endswith("/card.md"):
            docs += chunks(path, view.read(path) or "")
    return docs


def recall_tool(view: MemoryView) -> Tool:
    index = BM25(corpus(view))

    def run(args: dict, ctx: ToolContext) -> str:
        hits = index.search(args["query"], min(int(args.get("limit") or 5), 10))
        if not hits:
            return "nothing in memory matches. Memory is incomplete: read the code."
        parts = [f"memory:{c.path}" + (f" § {c.heading}" if c.heading else "")
                 + f"\n{c.text}" for _, c in hits]
        return ("\n\n".join(parts) + "\n\n(Memory is what earlier runs and the team "
                "recorded. Check it against the code before relying on it; cite code, "
                "not memory.)")

    return Tool("recall", "Search what Habodek remembers about this repository and its "
                "neighbours: the repo card, lessons from earlier threads (including "
                "unconfirmed ones), other projects' cards. Keyword search (BM25).",
                {"type": "object", "properties": {
                    "query": {"type": "string", "minLength": 2},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
                 "required": ["query"], "additionalProperties": False},
                run, reads=True)
