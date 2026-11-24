"""Jobs: loading job.json and rebuilding the workspace the control plane prepared."""

from __future__ import annotations

from pathlib import Path

from habodek.brain.codeintel.workspace import Workspace
from habodek.brain.contract import JobEnvelope, MapJob, ReviewJob, ThreadJob


def load_job(path: Path | str) -> ReviewJob | ThreadJob | MapJob:
    text = Path(path).read_text(encoding="utf-8")
    return JobEnvelope.model_validate_json(text).job


def save_job(job: ReviewJob | ThreadJob | MapJob, path: Path | str) -> None:
    Path(path).write_text(JobEnvelope(job=job).model_dump_json(indent=2), encoding="utf-8")


def workspace_from_job(job: ReviewJob | ThreadJob | MapJob) -> Workspace:
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


def attach_dependencies(job: ReviewJob | ThreadJob | MapJob, ws: Workspace,
                        internal_packages: tuple[str, ...], cache_dir: Path,
                        index_url: str = "", find_links: tuple[str, ...] = ()) -> list[str]:
    """Resolve internal dependency source for the pins at base and head (handoff §3.5).

    Used when the control plane did not list dependencies in the job: the brain reads
    the pins from the repository, takes the source from the job's venvs if that exact
    version is installed, else `pip download`s it from the configured index (Artifactory).
    Fills `job.workspace.dependencies` and adds the `dep:` roots. Returns the log.
    """
    from habodek.brain.codeintel import pydeps
    from habodek.brain.contract import DependencyTree

    if job.workspace.dependencies or not internal_packages or ws.repo is None:
        return []
    resolver = pydeps.SourceResolver(cache_dir, site_dirs=[Path(v) for v in job.workspace.venvs],
                                     index_url=index_url,
                                     find_links=[Path(p) for p in find_links])
    deps = pydeps.resolve_dependencies(pydeps.pins_at(ws.repo, ws.base_sha),
                                       pydeps.pins_at(ws.repo, ws.head_sha),
                                       internal_packages, resolver)
    for dep in deps:
        for label, path in dep.labels().items():
            ws.add_root(label, path)
            version = label.split("@", 1)[1]
            side = "both" if not dep.bumped else ("head" if version == dep.head_version
                                                  else "base")
            job.workspace.dependencies.append(DependencyTree(name=dep.name, version=version,
                                                             path=str(path), side=side))
    return resolver.log
