"""Turn a scenario into a job and a workspace, as the control plane would for a real MR.

For a review: worktrees for head and the merge base; internal dependency source at
the versions base and head pin (from the scenario's local wheel index); consumer
repositories the scenario names, at their ref (until the cartographer's reverse
dependency index exists, M10, the scenario hands them over explicitly).

For a thread event: the stored finding, the thread, and a workspace whose base is the
revision the finding was made at, so `diff(finding_sha, HEAD)` is its change map.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from eval.scenarios import specs
from habodek.brain.codeintel import pydeps
from habodek.brain.codeintel.workspace import Workspace
from habodek.brain.contract import (
    DependencyTree,
    Finding,
    MergeRequest,
    Note,
    ReviewJob,
    Revisions,
    ThreadJob,
)
from habodek.brain.contract import Workspace as WorkspaceSpec


def _sha(repo: Path, ref: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], check=True,
                          capture_output=True, text=True).stdout.strip()


def _worktree(repo: Path, ref: str, target: Path) -> Path:
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach",
                        str(target), ref], check=True, capture_output=True)
    return target


def prepare(build_dir: Path, sid: str, work_dir: Path,
            run_label: str = "run1") -> tuple[ReviewJob | ThreadJob, Workspace]:
    scenario = specs.SCENARIOS[sid]
    build_dir, work_dir = Path(build_dir), Path(work_dir) / f"s{sid}" / run_label
    repo = build_dir / "repos" / scenario["repo"]
    if scenario["kind"] == "review":
        base_ref, head_ref = scenario["base"], scenario["head"]
        finding = None
    else:
        finding_data = json.loads((build_dir / "findings" / f"{sid}.json").read_text())
        finding = Finding.model_validate(finding_data)
        base_ref, head_ref = finding.head_sha, scenario["head"]
    ws = Workspace.from_repo(repo, base_ref, head_ref, work_dir)

    resolver = pydeps.SourceResolver(work_dir / "deps-cache",
                                     find_links=[build_dir / "index"])
    deps = pydeps.resolve_dependencies(pydeps.pins_at(repo, ws.base_sha),
                                       pydeps.pins_at(repo, ws.head_sha),
                                       specs.INTERNAL_PACKAGES, resolver)
    trees: list[DependencyTree] = []
    for dep in deps:
        for label, path in dep.labels().items():
            ws.add_root(label, path)
            version = label.split("@", 1)[1]
            side = "both" if not dep.bumped else ("head" if version == dep.head_version
                                                  else "base")
            trees.append(DependencyTree(name=dep.name, version=version, path=str(path),
                                        side=side))
    others: dict[str, str] = {}
    for name, ref in (scenario.get("other_repos") or {}).items():
        path = _worktree(build_dir / "repos" / name, ref, work_dir / "repos" / name)
        ws.add_root(f"repo:{name}", path)
        others[name] = str(path)

    mr = scenario.get("mr") or {"title": finding.title if finding else ""}
    common = dict(
        job_id=f"s{sid}-{run_label}",
        mr=MergeRequest(project=f"scenarios/{scenario['repo']}", iid=int(sid),
                        title=mr.get("title", ""), description=mr.get("description", ""),
                        author=mr.get("author", "dev"), source_branch=head_ref,
                        target_branch="main"),
        revisions=Revisions(base_sha=ws.base_sha, head_sha=ws.head_sha),
        workspace=WorkspaceSpec(head=str(ws.head), base=str(ws.base), repo=str(repo),
                                dependencies=trees, other_repos=others),
    )
    if scenario["kind"] == "review":
        job: ReviewJob | ThreadJob = ReviewJob(**common)
    else:
        job = ThreadJob(event=scenario["event"], finding=finding,
                        notes=[Note(author=n["author"], body=n["body"])
                               for n in scenario.get("notes", [])], **common)
    return job, ws


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
