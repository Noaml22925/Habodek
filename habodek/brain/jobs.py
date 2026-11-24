"""Jobs: loading job.json and rebuilding the workspace the control plane prepared."""

from __future__ import annotations

from pathlib import Path

from habodek.brain.codeintel.workspace import Workspace
from habodek.brain.contract import JobEnvelope, ReviewJob, ThreadJob


def load_job(path: Path | str) -> ReviewJob | ThreadJob:
    text = Path(path).read_text(encoding="utf-8")
    return JobEnvelope.model_validate_json(text).job


def save_job(job: ReviewJob | ThreadJob, path: Path | str) -> None:
    Path(path).write_text(JobEnvelope(job=job).model_dump_json(indent=2), encoding="utf-8")


def workspace_from_job(job: ReviewJob | ThreadJob) -> Workspace:
    """The production path: rebuild the Workspace from what the control plane prepared."""
    spec = job.workspace
    ws = Workspace(head=Path(spec.head), repo=Path(spec.repo) if spec.repo else None,
                   base=Path(spec.base) if spec.base else None,
                   base_sha=job.revisions.base_sha, head_sha=job.revisions.head_sha)
    for tree in spec.dependencies:
        ws.add_root(f"dep:{tree.name}@{tree.version}", tree.path)
    for name, path in spec.other_repos.items():
        ws.add_root(f"repo:{name}", path)
    for i, site in enumerate(spec.venvs):
        ws.add_root(f"site:{i}", site)
    return ws
