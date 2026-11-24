"""Keeping a long investigation inside the context window, in two steps.

1. Above ~60% of the window, old tool *outputs* are elided to one line
   (`[elided: == api/x.py (lines 1-420 of 420) -- re-run if needed]`). The model's own
   turns, reasoning and notes stay.
2. Above ~80%, the model writes a structured summary of the work so far (established
   facts with file:line, working hypotheses marked unverified, open questions, files
   read), and the history is rebuilt as: the pinned task and evidence, the summary,
   the notes. The summary request is a normal turn with one terminal-like tool.

Invariants: the pinned prefix (system + task message) is never touched; an assistant
`tool_calls` turn and its `tool` results are kept or replaced together, never split.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

CHARS_PER_TOKEN = 3.2  # conservative for code-heavy text
ELIDE_AT = 0.60
SUMMARISE_AT = 0.80
KEEP_RECENT_TOOL_RESULTS = 6

SUMMARY_TOOL_NAME = "record_progress"
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "established": {"type": "array", "items": {"type": "string"},
                        "description": "Facts you verified, each with file:line evidence."},
        "hypotheses": {"type": "array", "items": {"type": "string"},
                       "description": "Working hypotheses, not yet verified."},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "files_read": {"type": "array", "items": {"type": "string"}},
        "next_step": {"type": "string"},
    },
    "required": ["established", "hypotheses", "open_questions", "files_read", "next_step"],
}
SUMMARY_PROMPT = (
    "Your context is nearly full. Record your progress now by calling "
    f"{SUMMARY_TOOL_NAME}. Older turns will be replaced by what you record, so include "
    "every fact you established with its file:line evidence, the hypotheses you are still "
    "testing (marked as unverified), open questions, the files you have read, and your "
    "next step. Do not call any other tool in this turn."
)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    chars = 0
    for message in messages:
        chars += len(message.get("content") or "")
        chars += len(message.get("reasoning_content") or "")
        for call in message.get("tool_calls") or []:
            chars += len(json.dumps(call))
    return int(chars / CHARS_PER_TOKEN) + 8 * len(messages)


def groups(messages: list[dict[str, Any]], pinned: int) -> list[list[int]]:
    """Indexes after the pinned prefix, grouped so a tool_calls turn and its results
    stay together."""
    out: list[list[int]] = []
    i = pinned
    while i < len(messages):
        group = [i]
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls"):
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                group.append(j)
                j += 1
            i = j
        else:
            i += 1
        out.append(group)
    return out


def _elided(content: str) -> str:
    first = content.strip().splitlines()[0] if content.strip() else ""
    return f"[elided to save context: {first[:160]} -- run the tool again if you need it]"


def elide_old_outputs(messages: list[dict[str, Any]], pinned: int,
                      keep_recent: int = KEEP_RECENT_TOOL_RESULTS) -> int:
    """Elide tool outputs except the most recent `keep_recent`. Returns how many."""
    tool_indexes = [i for i in range(pinned, len(messages)) if messages[i].get("role") == "tool"]
    count = 0
    for i in tool_indexes[:-keep_recent] if keep_recent else tool_indexes:
        content = messages[i].get("content") or ""
        if content.startswith("[elided"):
            continue
        messages[i] = {**messages[i], "content": _elided(content)}
        count += 1
    return count


def render_summary(args: dict[str, Any], notes: list[str]) -> str:
    def section(title: str, items: list[str]) -> str:
        body = "\n".join(f"- {item}" for item in items) if items else "- (none)"
        return f"{title}:\n{body}"

    parts = [
        "Summary of your work so far (older turns were replaced by it):",
        section("Established (verified, with evidence)", args.get("established") or []),
        section("Working hypotheses (UNVERIFIED)", args.get("hypotheses") or []),
        section("Open questions", args.get("open_questions") or []),
        "Files already read: " + (", ".join(args.get("files_read") or []) or "(none)"),
        f"Next step you planned: {args.get('next_step') or '(none)'}",
    ]
    if notes:
        parts.append(section("Your notes", notes))
    parts.append("Continue from here.")
    return "\n\n".join(parts)


def rebuild(messages: list[dict[str, Any]], pinned: int, summary: str) -> list[dict[str, Any]]:
    """Pinned prefix + the summary as a user message."""
    return [*messages[:pinned], {"role": "user", "content": summary}]


@dataclass
class CompactionState:
    elisions: int = 0
    summaries: int = 0
