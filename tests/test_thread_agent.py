"""The thread agent on scenarios 5-9 with a scripted model (M7)."""

from __future__ import annotations

import json

import chat_template as ct
import pytest
from scripted_model import ScriptedModel

from eval.scenarios import build as scenario_build
from eval.scenarios.prepare import prepare
from eval.scenarios.score import score_thread
from habodek.brain.llm import fake_gateway as fg
from habodek.brain.llm.client import LLMClient
from habodek.brain.llm.profiles import load_profiles
from habodek.brain.thread.agent import run_thread
from habodek.brain.trace.writer import TraceWriter

PROFILES = load_profiles(env={})


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return scenario_build.build(tmp_path_factory.mktemp("sc") / "build")


def validator(model, body):
    ct.render(PROFILES.get(model).name, body)


def thread(built, sid, tmp_path, mode="good"):
    model = ScriptedModel(built, mode)
    gateway = fg.FakeGateway(model, validator=validator)
    trace = TraceWriter(tmp_path / f"trace-{sid}-{mode}")
    client = LLMClient("http://g.invalid/v1", "k", profiles=PROFILES, trace=trace,
                       transport=gateway.transport(), sleep=lambda s: None)
    job, ws = prepare(built, sid, tmp_path / "work", mode)
    return run_thread(job, ws, client, trace), model, gateway


@pytest.mark.parametrize("sid,verdict", [("5", "fixed"), ("6", "fix_incomplete"),
                                         ("7", "stands"), ("8", "withdrawn"),
                                         ("9", "stands")])
def test_thread_verdicts(built, tmp_path, sid, verdict):
    result, model, gateway = thread(built, sid, tmp_path)
    expect = json.loads((built / "expected" / f"{sid}.json").read_text())
    score = score_thread(sid, expect, result)
    assert result.status == "ok" and score.passed, (score, result.thread)
    assert result.thread.verdict == verdict
    assert result.requests <= 30
    assert model.calls.count("thread") == 2 and model.calls.count("thread challenger") == 2
    priority = gateway.request_headers[0]["x-habodek-priority"]
    assert priority == ("recheck" if sid == "5" else "reply")


def test_fix_incomplete_reply_lists_remaining_sites(built, tmp_path):
    result, *_ = thread(built, "6", tmp_path)
    body = result.thread.body
    assert "orders-service `orders/jobs/digest.py:22`" in body
    assert "`platform_common/cache/users.py:27`" in body


def test_withdrawn_records_a_lesson(built, tmp_path):
    result, *_ = thread(built, "8", tmp_path)
    assert result.thread.lesson_candidate.startswith("Ownership checks")
    assert result.thread.convinced_by[0].path == "orders/app.py"


def test_chain_is_mapped_to_head_for_the_model(built, tmp_path):
    _, _, gateway = thread(built, "5", tmp_path)
    task = gateway.requests[0]["messages"][1]["content"]
    assert "-> now at platform_common/store/users.py:30 (changed since the finding)" in task
    assert "(quote still present)" in task  # the consumer repository links
    assert "+        raise NotFound" in task  # the diff since the finding


def test_persistent_disagreement_is_unclear_not_a_coin_toss(built, tmp_path):
    result, model, _ = thread(built, "9", tmp_path, "challenger_disagrees")
    assert result.thread.verdict == "unclear"
    assert "A human should decide" in result.thread.body
    assert model.calls.count("thread") == 4 and model.calls.count("thread challenger") == 4
    assert result.requests <= 30


def test_thread_scorecard_cli_over_http(built, tmp_path, monkeypatch, capsys):
    from habodek.brain import cli

    gateway = fg.FakeGateway(ScriptedModel(built), validator=validator)
    with gateway.serve() as url:
        monkeypatch.setenv("HABODEK_LLM_BASE_URL", url)
        monkeypatch.setenv("HABODEK_RPM", "6000")
        monkeypatch.setenv("HABODEK_HOME", str(tmp_path / "home"))
        code = cli.main(["scorecard", "--scenarios", "5-9", "--runs", "1", "--build",
                         str(built), "--out", str(tmp_path / "card")])
    lines = capsys.readouterr().out.strip().splitlines()
    assert code == 0 and len(lines) <= 20
    assert [line.split()[2] for line in lines[1:6]] == ["OK"] * 5, lines
    assert "verdicts 5/5" in lines[6]


def test_thread_cli_on_a_job_file(built, tmp_path, monkeypatch, capsys):
    from habodek.brain import cli
    from habodek.brain.jobs import save_job

    job, _ = prepare(built, "9", tmp_path / "work", "cli")
    save_job(job, tmp_path / "job.json")
    gateway = fg.FakeGateway(ScriptedModel(built), validator=validator)
    with gateway.serve() as url:
        monkeypatch.setenv("HABODEK_LLM_BASE_URL", url)
        monkeypatch.setenv("HABODEK_RPM", "6000")
        code = cli.main(["thread", "--job", str(tmp_path / "job.json"), "--out",
                         str(tmp_path / "result.json"), "--trace", str(tmp_path / "trace")])
    assert code == 0 and "stands" in capsys.readouterr().out
    assert json.loads((tmp_path / "result.json").read_text())["thread"]["verdict"] == "stands"
