"""What jobs take from memory, and what they give back.

In: the repo card and the active lessons that cover the changed paths, as one section
of every agent's context (kept short: the rest is behind `recall`). Out: a lesson
event from a thread whose `withdrawn` verdict survived the challenge.
"""

from __future__ import annotations

from datetime import UTC, datetime

from habodek.brain.contract import (
    Citation,
    LessonEvent,
    MemoryUpdate,
    ThreadJob,
    ThreadVerdict,
)
from habodek.brain.memory.store import MemoryView

CARD_CHARS = 6000


def open_view(job) -> MemoryView | None:
    """The job's memory at the commit the control plane chose (or HEAD), if any."""
    if not job.memory_path:
        return None
    try:
        view = MemoryView(job.memory_path, job.mr.project, job.memory_commit)
    except (OSError, RuntimeError):
        return None
    return None if view.empty else view


def memory_section(view: MemoryView | None, changed_paths: list[str] | None) -> str:
    if view is None:
        return ""
    parts = []
    card = view.card().strip()
    if card:
        if len(card) > CARD_CHARS:
            card = card[:CARD_CHARS] + "\n... (card cut; `recall` searches all of it)"
        parts.append(card)
    lessons = view.active_lessons(changed_paths)
    if lessons:
        parts.append("Lessons from earlier verified threads in this repository:\n"
                     + "\n".join(x.brief() for x in lessons))
    if not parts:
        return ""
    return ("What Habodek remembers about this repository (memory commit "
            f"{view.commit[:10]}). It is context, not evidence: check it against the code, "
            "and cite the code.\n\n" + "\n\n".join(parts))


def lesson_update(job: ThreadJob, verdict: ThreadVerdict) -> MemoryUpdate | None:
    """Only a verified withdrawal teaches something (never a one-off reply, a fix, or an
    unclear outcome)."""
    if verdict.verdict != "withdrawn" or not verdict.lesson_candidate:
        return None
    f = job.finding
    scope = f.anchor.path.rsplit("/", 1)[0] if "/" in f.anchor.path else ""
    return MemoryUpdate(kind="lesson", project=job.mr.project, lesson=LessonEvent(
        text=verdict.lesson_candidate, scope=scope,
        citation=Citation(mr=f"{job.mr.project}!{job.mr.iid}", finding_id=f.id,
                          verdict="withdrawn", head_sha=job.revisions.head_sha,
                          at=datetime.now(UTC).strftime("%Y-%m-%d"),
                          convinced_by=verdict.convinced_by)))
