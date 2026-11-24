"""The Go control plane and the brain agree on the JSON contract.

Go writes agent/internal/core/brain/testdata/job_*.json (TestGoldenJob); they must
validate here. The brain's result_review.json there comes from a real pipeline run on
scenario S1 (regenerate with UPDATE_GOLDEN=1) and is parsed by Go with
DisallowUnknownFields, so a field added on either side without the other fails a test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from habodek.brain.contract import (
    CONTRACT_VERSION,
    JobEnvelope,
    MapJob,
    Result,
    ReviewJob,
    ThreadJob,
)

TESTDATA = Path(__file__).resolve().parents[1] / "agent/internal/core/brain/testdata"


def test_go_review_job_validates():
    job = JobEnvelope.model_validate_json((TESTDATA / "job_review.json").read_text()).job
    assert isinstance(job, ReviewJob) and job.contract == CONTRACT_VERSION
    assert job.workspace.other_repos == {"orders-service": "/w/orders"}
    assert job.workspace.dependencies[0].side == "head"


def test_go_thread_job_validates():
    job = JobEnvelope.model_validate_json((TESTDATA / "job_thread.json").read_text()).job
    assert isinstance(job, ThreadJob) and job.event == "reply" and job.priority == "reply"
    assert job.finding.chain[0].repo == "repo:orders-service"
    assert job.notes[1].is_bot


def test_go_map_job_validates():
    job = JobEnvelope.model_validate_json((TESTDATA / "job_map.json").read_text()).job
    assert isinstance(job, MapJob) and job.priority == "background"
    assert job.mr.project_id == 12 and job.memory_commit == "c" * 40


def test_result_golden_is_a_valid_result():
    result = Result.model_validate_json((TESTDATA / "result_review.json").read_text())
    assert result.contract == CONTRACT_VERSION and len(result.comments) == 1


@pytest.mark.skipif(not os.environ.get("UPDATE_GOLDEN"), reason="set UPDATE_GOLDEN=1")
def test_regenerate_result_golden(tmp_path):
    from test_review_pipeline import review

    from eval.scenarios import build as scenario_build

    built = scenario_build.build(tmp_path / "build")
    result, *_ = review(built, "1", tmp_path)
    data = json.loads(result.model_dump_json())
    data["llm"] = {"requests": data["llm"]["requests"]}  # keep the golden small
    (TESTDATA / "result_review.json").write_text(json.dumps(data, indent=2) + "\n")
