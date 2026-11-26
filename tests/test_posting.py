"""Posting policy (design §6.10): open threads are not repeated, a repository's minimum
severity holds, and criticals never count against the comment budget."""

from __future__ import annotations

import json

import pytest
import test_review_pipeline as trp

from eval.scenarios import build as scenario_build
from habodek.brain.contract import Anchor, Finding, Policy
from habodek.brain.workflow.pipeline import Review


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return scenario_build.build(tmp_path_factory.mktemp("post") / "build")


def review_with(built, sid, tmp_path, **job_fields):
    original = trp.prepare

    def patched(*a, **k):
        job, ws = original(*a, **k)
        for key, value in job_fields.items():
            setattr(job, key, value)
        return job, ws

    trp.prepare = patched
    try:
        return trp.review(built, sid, tmp_path)
    finally:
        trp.prepare = original


def f1(built) -> Finding:
    return Finding.model_validate(json.loads((built / "findings" / "5.json").read_text()))


def test_a_rereview_does_not_repeat_an_open_thread(built, tmp_path):
    result, _, _, gateway = review_with(built, "1", tmp_path, open_findings=[f1(built)])
    assert not result.comments
    (held,) = [f for f in result.findings if f.dropped_at == "open_thread"]
    assert f1(built).id in held.drop_reason
    assert "Not repeated: 1 finding(s) already raised in open threads" in result.summary
    finder_tasks = [r["messages"][1]["content"] for r in gateway.requests
                    if r["messages"][0]["content"].startswith("Role: finder")]
    assert finder_tasks and all("Already raised in open threads" in t for t in finder_tasks)


def test_an_unrelated_open_thread_does_not_suppress_a_new_finding(built, tmp_path):
    result, *_ = review_with(built, "2", tmp_path, open_findings=[f1(built)])
    assert len(result.comments) == 1


def test_the_repository_minimum_severity(built, tmp_path):
    result, *_ = review_with(built, "1", tmp_path, policy=Policy(min_severity="critical"))
    assert not result.comments
    assert any(f.dropped_at == "policy" for f in result.findings)
    assert "Below this repository's minimum severity (critical): 1 not posted" in result.summary


def test_criticals_do_not_count_against_the_comment_budget():
    def f(i, sev):
        return Finding(id=f"f{i}", title="t", claim="c", severity=sev,
                       anchor=Anchor(path="a.py", line=i))

    review = Review.__new__(Review)
    review.job = type("J", (), {"max_comments": 1})()
    chosen = [f(1, "critical"), f(2, "major"), f(3, "critical"), f(4, "minor")]
    assert [x.id for x in review._within_budget(chosen)] == ["f1", "f2", "f3"]
