from __future__ import annotations

import json
import socket

import conftest
import pytest

from habodek.brain import cli
from habodek.brain.contract import JobEnvelope, ReviewJob, ThreadJob, finding_marker


def _job(kind: str) -> dict:
    base = {
        "kind": "review",
        "job_id": "j1",
        "mr": {"project": "team/orders-service", "iid": 7, "title": "Bump payments-lib"},
        "revisions": {"base_sha": "a" * 40, "head_sha": "b" * 40},
        "workspace": {"head": "/w/head", "base": "/w/base"},
    }
    if kind == "thread":
        base.update(kind="thread", event="reply", finding={
            "id": "F1", "title": "t", "claim": "c",
            "anchor": {"path": "store/users.py", "line": 41},
            "chain": [{"path": "api/profile.py", "line": 31, "quote": "user.email",
                       "claim": "dereferences None"}]})
    return base


def test_jobs_round_trip_with_discriminator():
    review = JobEnvelope.model_validate({"job": _job("review")}).job
    thread = JobEnvelope.model_validate({"job": _job("thread")}).job
    assert isinstance(review, ReviewJob) and review.priority == "review"
    assert isinstance(thread, ThreadJob) and thread.priority == "reply"
    again = JobEnvelope.model_validate_json(JobEnvelope(job=thread).model_dump_json())
    assert again.job.finding.chain[0].line == 31


def test_unknown_fields_are_rejected():
    bad = _job("review")
    bad["surprise"] = 1
    with pytest.raises(ValueError):
        JobEnvelope.model_validate({"job": bad})


def test_schema_export(tmp_path, capsys):
    assert cli.main(["schema", "--out", str(tmp_path)]) == 0
    schema = json.loads((tmp_path / "job.schema.json").read_text())
    assert "ReviewJob" in json.dumps(schema) and "ThreadJob" in json.dumps(schema)
    assert (tmp_path / "result.schema.json").exists()


def test_unimplemented_commands_say_which_milestone(capsys):
    assert cli.main(["map", "--job", "x.json"]) == 2
    assert "M10" in capsys.readouterr().err


def test_marker():
    assert finding_marker("F1") == "<!-- habodek:finding:F1 -->"


def test_audit_hook_blocks_non_loopback_connections():
    before = len(conftest._violations)
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("10.255.255.1", 80), timeout=0.1)
    # This test provoked the violation on purpose; do not fail the teardown check.
    del conftest._violations[before:]
