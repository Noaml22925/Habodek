"""Rendering a verified finding as a GitLab comment, from its structured fields.

The body is built by code from the finding the checks and verifiers passed — the
claim, the evidence chain with `path:line` per link, the failing scenario and the fix —
so nothing in the comment is unverified prose. Each `path:line` of the reviewed
repository links to the blob at the head SHA when the MR's web URL is known.
"""

from __future__ import annotations

from habodek.brain.contract import Comment, EvidenceLink, Finding, MergeRequest, finding_marker

SEVERITY_MARK = {"critical": "🔴", "major": "🟠", "minor": "🟡"}


def _project_url(mr: MergeRequest) -> str:
    return mr.web_url.split("/-/merge_requests")[0] if "/-/merge_requests" in mr.web_url \
        else ""


def where(link: EvidenceLink, mr: MergeRequest | None = None, head_sha: str = "") -> str:
    label = f"{link.path}:{link.line}"
    if link.repo:
        name = link.repo.split(":", 1)[1] if ":" in link.repo else link.repo
        return f"{name} `{label}`"
    if link.revision == "base":
        return f"`{label}` (before the change)"
    base = _project_url(mr) if mr else ""
    if base and head_sha:
        return f"[`{label}`]({base}/-/blob/{head_sha}/{link.path}#L{link.line})"
    return f"`{label}`"


def render_body(finding: Finding, mr: MergeRequest | None, head_sha: str, run_id: str,
                checked: str = "") -> str:
    mark = SEVERITY_MARK.get(finding.severity, "🟠")
    lines = [f"{mark} **{finding.title.strip().rstrip('.')}.**", "", finding.claim.strip(), ""]
    for link in finding.chain:
        claim = link.claim.strip() or "see code"
        lines.append(f"- {where(link, mr, head_sha)} — `{link.quote.strip()[:120]}` — {claim}")
    if finding.scenario.strip():
        lines += ["", f"**Scenario:** {finding.scenario.strip()}"]
    if finding.fix.strip():
        lines += ["", f"**Fix:** {finding.fix.strip()}"]
    upheld = sum(1 for v in finding.verifications if v.outcome == "upheld")
    footer = [checked] if checked else []
    if upheld >= 2:
        footer.append("upheld by two independent verifiers")
    footer.append(f"trace `{run_id}`")
    lines += ["", "<sub>" + " · ".join(footer) + "</sub>", "", finding_marker(finding.id)]
    return "\n".join(lines)


def to_comment(finding: Finding, mr: MergeRequest | None, head_sha: str, run_id: str,
               checked: str = "") -> Comment:
    return Comment(finding_id=finding.id, path=finding.anchor.path, line=finding.anchor.line,
                   side=finding.anchor.side,
                   body=render_body(finding, mr, head_sha, run_id, checked),
                   marker=finding_marker(finding.id))
