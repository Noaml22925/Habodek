"""The brain's contract with the control plane: `job.json` in, `result.json` out.

The control plane fetches the bytes (worktrees, dependency source, the thread) and
posts the comments; the brain decides. Nothing here talks to GitLab.

`habodek-brain schema` exports these as JSON Schema for the Go side.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = 1

Severity = Literal["critical", "major", "minor"]
Side = Literal["new", "old"]
Priority = Literal["reply", "recheck", "review", "background"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------- shared pieces


class MergeRequest(_Model):
    project: str  # path_with_namespace
    iid: int
    title: str = ""
    description: str = ""
    author: str = ""
    labels: list[str] = Field(default_factory=list)
    source_branch: str = ""
    target_branch: str = ""
    web_url: str = ""
    draft: bool = False


class Revisions(_Model):
    base_sha: str  # merge base
    head_sha: str
    start_sha: str = ""  # GitLab's diff_refs.start_sha


class DependencyTree(_Model):
    """Source of a dependency at the version a revision pins."""

    name: str
    version: str
    path: str
    side: Literal["head", "base", "both"] = "both"


class Workspace(_Model):
    head: str  # worktree at head_sha
    base: str | None = None  # worktree at base_sha (created lazily if absent)
    repo: str | None = None  # a git repository holding both revisions (for log/blame/diff)
    dependencies: list[DependencyTree] = Field(default_factory=list)
    venvs: list[str] = Field(default_factory=list)  # site-packages roots to resolve into
    other_repos: dict[str, str] = Field(default_factory=dict)  # name -> path (consumers etc.)


class EvidenceLink(_Model):
    """One step of a chain from the changed line to the failure."""

    path: str
    line: int
    quote: str  # the code at that line, verbatim
    claim: str  # what this line contributes to the failure
    revision: Literal["head", "base"] = "head"
    repo: str | None = None  # None: the reviewed repository


class Anchor(_Model):
    path: str
    line: int
    side: Side = "new"
    snippet: str = ""  # the quoted changed code the anchor was resolved from


class Verification(_Model):
    verifier: str  # role, e.g. "verifier_a"
    model: str
    outcome: Literal["upheld", "refuted", "uncertain", "error"]
    reason: str = ""
    evidence: list[EvidenceLink] = Field(default_factory=list)


class Finding(_Model):
    id: str
    title: str
    severity: Severity = "major"
    category: str = ""
    claim: str
    scenario: str = ""  # a concrete failing scenario
    fix: str = ""
    anchor: Anchor
    chain: list[EvidenceLink] = Field(default_factory=list)
    head_sha: str = ""  # the revision the finding was made at
    verifications: list[Verification] = Field(default_factory=list)
    status: Literal["posted", "dropped", "not_investigated"] = "posted"
    dropped_at: str | None = None  # stage that dropped it
    drop_reason: str | None = None


class Note(_Model):
    author: str
    body: str
    created_at: str = ""
    is_bot: bool = False


class ModelRouting(_Model):
    """Optional per-job override of models.yaml roles."""

    roles: dict[str, str] = Field(default_factory=dict)


# ----------------------------------------------------------------- jobs


class _JobBase(_Model):
    contract: int = CONTRACT_VERSION
    job_id: str
    mr: MergeRequest
    revisions: Revisions
    workspace: Workspace
    priority: Priority = "review"
    request_budget: int | None = None  # None: the default for the job kind
    memory_path: str | None = None
    models: ModelRouting = Field(default_factory=ModelRouting)


class ReviewJob(_JobBase):
    kind: Literal["review"] = "review"
    max_comments: int = 6
    open_findings: list[Finding] = Field(default_factory=list)  # earlier threads, not repeated


class ThreadJob(_JobBase):
    kind: Literal["thread"] = "thread"
    event: Literal["reply", "push"]
    finding: Finding
    notes: list[Note] = Field(default_factory=list)  # the whole thread, oldest first
    priority: Priority = "reply"


Job = Annotated[ReviewJob | ThreadJob, Field(discriminator="kind")]


class JobEnvelope(_Model):
    job: Job


# ----------------------------------------------------------------- result


class Comment(_Model):
    finding_id: str
    path: str
    line: int
    side: Side = "new"
    body: str
    marker: str  # hidden idempotency marker, e.g. <!-- habodek:finding:<id> -->


class ThreadVerdict(_Model):
    verdict: Literal["fixed", "fix_incomplete", "stands", "withdrawn", "need_info", "unclear"]
    body: str  # the reply to post
    remaining: list[EvidenceLink] = Field(default_factory=list)  # for fix_incomplete / stands
    convinced_by: list[EvidenceLink] = Field(default_factory=list)  # for withdrawn
    lesson_candidate: str | None = None


class StageStat(_Model):
    stage: str
    requests: int = 0
    agents: int = 0
    seconds: float = 0.0
    notes: str = ""


class Result(_Model):
    contract: int = CONTRACT_VERSION
    job_id: str
    run_id: str
    kind: Literal["review", "thread", "map"]
    status: Literal["ok", "skipped", "failed"]
    reason: str = ""  # for skipped / failed
    comments: list[Comment] = Field(default_factory=list)
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)  # posted and dropped, with reasons
    thread: ThreadVerdict | None = None
    not_investigated: list[str] = Field(default_factory=list)  # "budget" leftovers
    stats: list[StageStat] = Field(default_factory=list)
    requests: int = 0
    llm: dict[str, Any] = Field(default_factory=dict)  # client stats


def finding_marker(finding_id: str) -> str:
    return f"<!-- habodek:finding:{finding_id} -->"


def json_schemas() -> dict[str, Any]:
    return {
        "job": JobEnvelope.model_json_schema(),
        "result": Result.model_json_schema(),
    }
