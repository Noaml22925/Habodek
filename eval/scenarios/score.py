"""Score a brain result against a scenario's expectations.

First half (reviews):
- found or missed: a *posted* finding about the expected root cause (anchored in the
  expected file, or citing the expected anchor line in its chain);
- chain correct: every expected site is cited in that finding's evidence chain;
- anchor correct: the comment sits on the expected changed line;
- silent: nothing posted on a clean scenario.
For a miss, the stage that lost it: not hypothesised / not investigated (budget) /
dropped at a stage (investigation refuted, a mechanical check, a verifier).

Second half (threads): the verdict, and for `fix_incomplete` / `stands` the cited
remaining site; for `withdrawn` with `lesson`, that a lesson candidate was recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from habodek.brain.contract import EvidenceLink, Finding, Result


@dataclass
class Site:
    repo: str | None  # None: the reviewed repository
    path: str


def site_of(link: EvidenceLink | dict[str, Any]) -> Site:
    """Normalise a chain link or expected site: path specs carry their root."""
    if isinstance(link, EvidenceLink):
        repo, path = link.repo, link.path
    else:
        repo, path = link.get("repo"), link["path"]
    if ":" in path.split("/")[0]:
        label, _, rest = path.partition(":")
        if label in ("repo", "dep", "site"):
            name, _, rest = rest.partition(":")
            repo = name.split("@")[0] if label != "site" else repo
            path = rest
        elif label in ("head", "base"):
            path = rest
    if repo and repo.startswith(("repo:", "dep:")):
        repo = repo.split(":", 1)[1].split("@")[0]
    return Site(repo or None, path.lstrip("./"))


def site_matches(expected: dict[str, Any], link: EvidenceLink | dict[str, Any]) -> bool:
    want, got = site_of(expected), site_of(link)
    return want.path == got.path and (want.repo or None) == (got.repo or None)


@dataclass
class ReviewScore:
    sid: str
    expected: str  # found | silent
    outcome: str  # FOUND | MISS | SILENT | NOISY
    posted: int
    chain_ok: bool = False
    anchor_ok: bool = False
    missing_sites: list[str] = field(default_factory=list)
    lost_at: str = ""
    hypotheses: int = 0
    refuted: int = 0
    requests: int = 0

    @property
    def passed(self) -> bool:
        if self.expected == "silent":
            return self.outcome == "SILENT"
        return self.outcome == "FOUND" and self.chain_ok and self.anchor_ok


def _about_root_cause(finding: Finding, expect: dict[str, Any]) -> bool:
    anchor = expect["anchor"]
    if finding.anchor.path == anchor["path"]:
        return True
    return any(site_matches({"path": anchor["path"]}, link) and abs(link.line - anchor["line"])
               <= 2 for link in finding.chain)


def score_review(sid: str, expect: dict[str, Any], result: Result) -> ReviewScore:
    posted = [f for f in result.findings if f.status == "posted"]
    posted_count = len(result.comments)
    dropped = [f for f in result.findings if f.status != "posted"]
    score = ReviewScore(sid, expect["outcome"], "", posted_count,
                        hypotheses=len(result.findings),
                        refuted=len([f for f in dropped if f.status == "dropped"]),
                        requests=result.requests)
    if expect["outcome"] == "silent":
        score.outcome = "SILENT" if posted_count == 0 else "NOISY"
        if posted_count:
            score.lost_at = "posted: " + "; ".join(f.title[:60] for f in posted[:2])
        return score
    match = next((f for f in posted if _about_root_cause(f, expect)), None)
    if match is None:
        score.outcome = "MISS"
        lost = next((f for f in dropped if _about_root_cause(f, expect)), None)
        if lost is None:
            score.lost_at = "not hypothesised"
        elif lost.status == "not_investigated":
            score.lost_at = "not investigated: budget"
        else:
            score.lost_at = f"dropped at {lost.dropped_at}: {lost.drop_reason or ''}".strip()
        return score
    score.outcome = "FOUND"
    missing = [s for s in expect.get("sites", [])
               if not any(site_matches(s, link) for link in match.chain)]
    score.missing_sites = [(f"{s['repo']}:" if s.get("repo") else "") + s["path"]
                           for s in missing]
    score.chain_ok = not missing
    comment = next((c for c in result.comments if c.finding_id == match.id), None)
    score.anchor_ok = bool(comment and comment.path == expect["anchor"]["path"]
                           and comment.line == expect["anchor"]["line"])
    if posted_count > expect.get("max_comments", 1):
        score.lost_at = f"{posted_count} comments posted, expected {expect['max_comments']}"
    return score


@dataclass
class ThreadScore:
    sid: str
    expected: str
    got: str
    verdict_ok: bool
    sites_ok: bool
    missing_sites: list[str] = field(default_factory=list)
    lesson_ok: bool = True
    requests: int = 0

    @property
    def passed(self) -> bool:
        return self.verdict_ok and self.sites_ok and self.lesson_ok


def score_thread(sid: str, expect: dict[str, Any], result: Result) -> ThreadScore:
    verdict = result.thread.verdict if result.thread else "none"
    cited = list(result.thread.remaining) if result.thread else []
    missing = [s for s in expect.get("sites", [])
               if not any(site_matches(s, link) for link in cited)]
    lesson_ok = True
    if expect.get("lesson"):
        lesson_ok = bool(result.thread and result.thread.lesson_candidate)
    return ThreadScore(sid, expect["verdict"], verdict, verdict == expect["verdict"],
                       not missing,
                       [(f"{s['repo']}:" if s.get("repo") else "") + s["path"] for s in missing],
                       lesson_ok, result.requests)
