"""Scenario fixtures, the scorer and the scorecard (M5)."""

from __future__ import annotations

import json
import subprocess

import pytest

from eval.scenarios import build as scenario_build
from eval.scenarios import specs
from eval.scenarios.prepare import prepare, workspace_from_job
from eval.scenarios.score import score_review, score_thread, site_matches
from habodek.brain.contract import (
    Anchor,
    Comment,
    EvidenceLink,
    Finding,
    Result,
    ThreadVerdict,
    finding_marker,
)
from habodek.brain.trace.scorecard import run_scorecard


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return scenario_build.build(tmp_path_factory.mktemp("scenarios") / "build")


def expected(built, sid):
    return json.loads((built / "expected" / f"{sid}.json").read_text())


def line_of(built, repo, ref, path, text):
    return scenario_build.resolve_line(built / "repos" / repo, ref, path, text)


def _refs(root, repo):
    out = subprocess.run(["git", "-C", str(root / "repos" / repo), "show-ref"],
                         capture_output=True, text=True, check=True).stdout
    return sorted(out.splitlines())


def test_build_is_deterministic(built, tmp_path):
    again = scenario_build.build(tmp_path / "again")
    for repo in ("platform-common", "payments-lib", "orders-service"):
        assert _refs(built, repo) == _refs(again, repo)
    assert sorted(p.name for p in (built / "index").iterdir()) == [
        "payments_lib-1.4.2-py3-none-any.whl", "payments_lib-1.5.0-py3-none-any.whl",
        "platform_common-2.3.0-py3-none-any.whl"]


def test_prepare_review_job_with_deps_and_consumers(built, tmp_path):
    job, ws = prepare(built, "2", tmp_path)
    labels = set(ws.roots())
    assert {"dep:payments-lib@1.5.0", "dep:payments-lib@1.4.2",
            "dep:platform-common@2.3.0"} <= labels
    assert ws.changed_paths() == ["requirements.txt"]
    client = ws.resolve("dep:payments-lib@1.5.0:payments/client.py").read_text()
    assert 'return ChargeResult(response.get("id", ""), "declined"' in client
    again = workspace_from_job(job)
    assert set(again.roots()) == labels
    job1, ws1 = prepare(built, "1", tmp_path)
    assert "repo:orders-service" in ws1.roots()
    assert ws1.resolve("repo:orders-service:orders/jobs/digest.py").is_file()


def test_prepare_thread_job(built, tmp_path):
    job, ws = prepare(built, "5", tmp_path)
    assert job.kind == "thread" and job.finding.id == "F1"
    assert ws.base_sha == job.finding.head_sha  # diff(finding_sha, HEAD)
    assert ws.changed_paths() == ["platform_common/store/users.py", "tests/test_cache.py",
                                  "tests/test_users.py"]
    job6, ws6 = prepare(built, "6", tmp_path)
    assert ws6.changed_paths() == []  # nothing pushed to the library
    profile = ws6.resolve("repo:orders-service:orders/api/profile.py").read_text()
    assert "if user is None:" in profile


# ----------------------------------------------------------------------- scorer


def s1_result(built, *, sites=("cache", "profile", "digest"), anchor_delta=0,
              extra_comment=False) -> Result:
    head = specs.SCENARIOS["1"]["head"]
    exp = expected(built, "1")
    chain = [EvidenceLink(path="platform_common/store/users.py", line=exp["anchor"]["line"],
                          quote="return None", claim="returns None")]
    if "cache" in sites:
        chain.append(EvidenceLink(
            path="platform_common/cache/users.py",
            line=line_of(built, "platform-common", head, "platform_common/cache/users.py",
                         "except NotFound:"), quote="except NotFound:", claim="no neg cache"))
    if "profile" in sites:
        chain.append(EvidenceLink(path="repo:orders-service:orders/api/profile.py", line=13,
                                  quote="user.email", claim="AttributeError"))
    if "digest" in sites:
        chain.append(EvidenceLink(path="orders/jobs/digest.py", repo="orders-service", line=22,
                                  quote="user.email", claim="digest dies"))
    finding = Finding(id="f1", title="get_user returns None", claim="c",
                      anchor=Anchor(path="platform_common/store/users.py",
                                    line=exp["anchor"]["line"] + anchor_delta), chain=chain)
    comments = [Comment(finding_id="f1", path=finding.anchor.path, line=finding.anchor.line,
                        body="b", marker=finding_marker("f1"))]
    findings = [finding]
    if extra_comment:
        other = Finding(id="f2", title="nit", claim="c", anchor=Anchor(path="x.py", line=1))
        findings.append(other)
        comments.append(Comment(finding_id="f2", path="x.py", line=1, body="b",
                                marker=finding_marker("f2")))
    return Result(job_id="j", run_id="r", kind="review", status="ok", comments=comments,
                  findings=findings, requests=200)


def test_score_s1_correct(built):
    score = score_review("1", expected(built, "1"), s1_result(built))
    assert score.passed and score.outcome == "FOUND", score


def test_score_s1_chain_missing_a_site(built):
    score = score_review("1", expected(built, "1"), s1_result(built, sites=("cache",
                                                                            "profile")))
    assert not score.passed and not score.chain_ok
    assert score.missing_sites == ["orders-service:orders/jobs/digest.py"]


def test_score_s1_anchor_off(built):
    score = score_review("1", expected(built, "1"), s1_result(built, anchor_delta=-1))
    assert score.outcome == "FOUND" and not score.anchor_ok and not score.passed


def test_score_s1_extra_comment_is_reported(built):
    score = score_review("1", expected(built, "1"), s1_result(built, extra_comment=True))
    assert "2 comments posted" in score.lost_at


def test_score_miss_names_the_stage(built):
    good = s1_result(built)
    dropped = good.model_copy(deep=True)
    dropped.comments = []
    dropped.findings[0].status = "dropped"
    dropped.findings[0].dropped_at = "verify_a"
    dropped.findings[0].drop_reason = "refuted: guarded upstream"
    score = score_review("1", expected(built, "1"), dropped)
    assert score.outcome == "MISS" and score.lost_at.startswith("dropped at verify_a")
    nothing = Result(job_id="j", run_id="r", kind="review", status="ok")
    assert score_review("1", expected(built, "1"), nothing).lost_at == "not hypothesised"


def test_score_clean_scenarios(built):
    silent = Result(job_id="j", run_id="r", kind="review", status="ok",
                    findings=[Finding(id="h1", title="maybe None", claim="c",
                                      anchor=Anchor(path="orders/validation.py", line=20),
                                      status="dropped", dropped_at="investigate")])
    score = score_review("3", expected(built, "3"), silent)
    assert score.passed and score.outcome == "SILENT" and score.refuted == 1
    noisy = s1_result(built)
    assert score_review("3", expected(built, "3"), noisy).outcome == "NOISY"


def thread_result(verdict, remaining=(), lesson=None) -> Result:
    return Result(job_id="j", run_id="r", kind="thread", status="ok",
                  thread=ThreadVerdict(verdict=verdict, body="b", lesson_candidate=lesson,
                                       remaining=[EvidenceLink(path=p, repo=r, line=1, quote="q",
                                                               claim="c")
                                                  for r, p in remaining]))


def test_score_threads(built):
    digest = [("orders-service", "orders/jobs/digest.py")]
    assert score_thread("6", expected(built, "6"),
                        thread_result("fix_incomplete", digest)).passed
    assert not score_thread("6", expected(built, "6"), thread_result("fix_incomplete")).passed
    assert not score_thread("6", expected(built, "6"), thread_result("fixed")).passed
    assert score_thread("5", expected(built, "5"), thread_result("fixed")).passed
    assert score_thread("9", expected(built, "9"),
                        thread_result("stands", [(None, "orders/app.py")])).passed
    assert not score_thread("8", expected(built, "8"), thread_result("withdrawn")).passed
    assert score_thread("8", expected(built, "8"),
                        thread_result("withdrawn", lesson="middleware covers v1")).passed


def test_site_matching_normalises_path_specs():
    assert site_matches({"repo": "payments-lib", "path": "payments/client.py"},
                        EvidenceLink(path="dep:payments-lib@1.5.0:payments/client.py", line=1,
                                     quote="q", claim="c"))
    assert site_matches({"path": "orders/app.py"},
                        EvidenceLink(path="head:orders/app.py", line=1, quote="q", claim="c"))
    assert not site_matches({"path": "orders/app.py"},
                            EvidenceLink(path="repo:x:orders/app.py", line=1, quote="q",
                                         claim="c"))


def test_scorecard_prints_at_most_20_diagnostic_lines(built, tmp_path):
    def runner(job, ws, run_dir):
        sid = job.job_id.split("-")[0][1:]
        if sid == "1":
            return s1_result(built)
        if sid == "6":
            return thread_result("fix_incomplete")  # cites nothing: wrong
        return Result(job_id=job.job_id, run_id="r", kind="review", status="ok",
                      requests=100)

    lines = run_scorecard(["1", "2", "3", "6"], 2, tmp_path / "card", built, runner,
                          "inv=glm fin=qwen")
    assert len(lines) <= 20
    text = "\n".join(lines)
    assert lines[1].startswith("S1 cross-lib") and "FOUND  2/2  chain=2/2 anchor=2/2" in lines[1]
    assert "MISS" in lines[2] and "miss: not hypothesised" in lines[2]
    assert "SILENT 2/2" in lines[3]
    assert "WRONG" in lines[4] and "not cited: orders-service:orders/jobs/digest.py" in lines[4]
    assert "found 2/4" in text and "silent 2/2" in text and "verdicts 0/2" in text
    assert lines[-1] == f"details: {tmp_path / 'card'}"
    assert (tmp_path / "card" / "s1-side-by-side.md").is_file()
