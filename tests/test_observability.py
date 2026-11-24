"""Observability (M11): run.json, metrics, bars, trace show, replay, probe.

Runs are recorded through the real CLI against the scripted gateway over HTTP, the
way the control plane runs the brain; then their worktrees are deleted, as the
control plane deletes them, so replay and probe must rebuild them from run.json.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import chat_template as ct
import pytest
from scripted_model import ScriptedModel
from test_review_pipeline import expected, review
from typer.testing import CliRunner

from eval.scenarios import build as scenario_build
from eval.scenarios.prepare import prepare
from habodek.brain import cli
from habodek.brain.contract import CategoryBar, Policy, Result
from habodek.brain.jobs import save_job
from habodek.brain.llm import fake_gateway as fg
from habodek.brain.llm.profiles import load_profiles
from habodek.brain.sandbox import local_available
from habodek.brain.trace import replay as rp
from habodek.brain.trace.view import render
from habodek.cli import app as habodek_app

PROFILES = load_profiles(env={})


def validator(model, body):
    ct.render(PROFILES.get(model).name, body)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return scenario_build.build(tmp_path_factory.mktemp("obs") / "build")


def record(built, base, sid, monkeypatch, sandbox="none"):
    """Run a scenario through `habodek-brain`, then delete its worktrees."""
    job, ws = prepare(built, sid, base / "work", "rec")
    save_job(job, base / "job.json")
    run_dir = base / "run"
    gateway = fg.FakeGateway(ScriptedModel(built), validator=validator)
    with gateway.serve() as url, monkeypatch.context() as m:
        m.setenv("HABODEK_LLM_BASE_URL", url)
        m.setenv("HABODEK_RPM", "6000")
        m.setenv("HABODEK_SANDBOX", sandbox)
        m.setenv("HABODEK_SANDBOX_CACHE", str(base / "sandbox-cache"))
        m.setenv("HABODEK_PIP_FIND_LINKS", str(built / "index"))
        code = cli.main([job.kind, "--job", str(base / "job.json"), "--out",
                         str(run_dir / "result.json"), "--trace", str(run_dir)])
    assert code == 0
    for path in (ws.head, ws.base, *ws.extra_roots.values()):
        if path and "work" in str(path) and path.exists() and (path / ".git").is_file():
            shutil.rmtree(path)
    for repo in (built / "repos").iterdir():
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], check=True)
    return run_dir, len(gateway.requests)


@pytest.fixture(scope="module")
def s2_run(built, tmp_path_factory):
    mp = pytest.MonkeyPatch()
    try:
        yield record(built, tmp_path_factory.mktemp("s2rec"), "2", mp)
    finally:
        mp.undo()


# --------------------------------------------------------------------------- inputs


def test_run_json_records_what_replay_needs(s2_run):
    run_dir, _ = s2_run
    info = rp.read_run(run_dir)
    assert info["kind"] == "review" and info["job"]["job_id"] == "s2-rec"
    head = info["roots"]["head"]
    assert len(head["sha"]) == 40 and head["repo"].endswith("orders-service")
    assert info["roots"]["base"]["sha"] == info["job"]["revisions"]["base_sha"]
    assert "dep:payments-lib@1.5.0" in info["roots"]
    assert info["roles"]["investigator"] and len(info["prompts"]["investigator"]) == 12
    assert info["sandbox"] == "none"


def test_metrics_funnel(s2_run):
    run_dir, _ = s2_run
    m = json.loads((run_dir / "metrics.json").read_text())
    assert m["funnel"]["posted"] == 1 and m["funnel"]["raised"] >= 1
    assert m["by_lens"]["consumers"]["posted"] == 1
    findings = json.loads((run_dir / "findings.json").read_text())
    assert all(f["hypothesis"].startswith("H") for f in findings)


def test_a_raised_bar_holds_back_lower_severities(built, tmp_path):
    job_bar = Policy(bars=[CategoryBar(category="intent", min_severity="critical",
                                       acceptance=0.3, outcomes=10)])
    import test_review_pipeline as trp

    original = trp.prepare

    def with_policy(*a, **k):
        job, ws = original(*a, **k)
        job.policy = job_bar
        return job, ws

    trp.prepare = with_policy
    try:
        result, _, trace, _ = review(built, "1", tmp_path)
    finally:
        trp.prepare = original
    assert not result.comments
    (held,) = [f for f in result.findings if f.dropped_at == "bar"]
    assert "acceptance 30% over 10" in held.drop_reason
    assert "Held back by a raised bar (intent (30% accepted, critical+))" in result.summary
    assert json.loads((trace.dir / "metrics.json").read_text())["funnel"]["posted"] == 0
    # a category without a bar is untouched
    result2, *_ = review(built, "2", tmp_path)
    assert result2.comments and expected(built, "2")


# --------------------------------------------------------------------------- trace show


def test_trace_show_renders_the_hypothesis_tree(s2_run, tmp_path):
    run_dir, _ = s2_run
    page = render(run_dir)
    assert page.startswith("<!doctype html>") and "<script" not in page
    for want in ("Hypothesis tree", "Posted as", "verifier_a", "upheld", "Funnel",
                 "investigate/H", "references", "Concluded", "Evidence pack",
                 "payments-lib 1.5.0 returns declined charges"):
        assert want in page, want
    assert "<details id='agent-investigate-H" in page
    out = CliRunner().invoke(habodek_app, ["trace", "show", str(run_dir), "--out",
                                           str(tmp_path / "t.html")])
    assert out.exit_code == 0 and (tmp_path / "t.html").read_text() == page


def test_trace_show_escapes_model_text(tmp_path):
    (tmp_path / "events.jsonl").write_text(json.dumps(
        {"seq": 1, "ts": 0, "kind": "triage", "kept": ["H1"], "hypotheses": [
            {"id": "H1", "claim": "<script>alert(1)</script>", "lenses": ["x"],
             "severity": "major", "why": "", "changed": ""}]}) + "\n")
    page = render(tmp_path)
    assert "<script>alert" not in page and "&lt;script&gt;" in page


# --------------------------------------------------------------------------- replay


def test_pure_replay_reproduces_the_run_from_the_cassette(s2_run, tmp_path, capsys, monkeypatch):
    run_dir, n = s2_run
    monkeypatch.delenv("HABODEK_LLM_BASE_URL", raising=False)
    code = cli.main(["replay", str(run_dir), "--out", str(tmp_path / "r")])
    out = capsys.readouterr().out
    assert code == 0, out
    assert f"cassette {n} hit(s), live 0, missed 0" in out and "same comments" in out
    assert "root head: rebuilt at" in out
    assert len(out.strip().splitlines()) <= 20
    result = Result.model_validate_json((tmp_path / "r/trace/result.json").read_text())
    assert result.comments and (tmp_path / "r/trace/run.json").is_file()


def test_replay_reports_where_the_harness_diverged(s2_run, tmp_path, capsys, monkeypatch):
    run_dir, _ = s2_run
    broken = tmp_path / "broken"
    shutil.copytree(run_dir, broken)
    lines = (broken / "events.jsonl").read_text().splitlines()
    resp = [i for i, ln in enumerate(lines) if '"kind": "llm_response"' in ln]
    seqs = {}
    for ln in lines:
        e = json.loads(ln)
        if e["kind"] == "llm_request" and e.get("run", "").startswith("verifier_a"):
            seqs[e["seq"]] = 1
    drop = next(i for i in resp if json.loads(lines[i])["request_seq"] in seqs)
    (broken / "events.jsonl").write_text("\n".join(lines[:drop] + lines[drop + 1:]) + "\n")
    monkeypatch.delenv("HABODEK_LLM_BASE_URL", raising=False)
    code = cli.main(["replay", str(broken), "--out", str(tmp_path / "r")])
    out = capsys.readouterr().out
    assert code == 1 and "first miss: verifier" in out


def test_replay_live_from_a_stage_and_with_another_model(s2_run, built, tmp_path, capsys,
                                                         monkeypatch):
    run_dir, n = s2_run
    gateway = fg.FakeGateway(ScriptedModel(built), validator=validator)
    with gateway.serve() as url:
        monkeypatch.setenv("HABODEK_LLM_BASE_URL", url)
        monkeypatch.setenv("HABODEK_RPM", "6000")
        assert cli.main(["replay", str(run_dir), "--out", str(tmp_path / "a"),
                         "--from-stage", "verify"]) == 0
        staged = capsys.readouterr().out
        live_roles = {r["messages"][0]["content"].splitlines()[0] for r in gateway.requests}
        assert live_roles <= {"Role: verifier", "Role: judge"}
        gateway.requests.clear()
        assert cli.main(["replay", str(run_dir), "--out", str(tmp_path / "b"),
                         "--model", "verifier_a=glm-5.3-flash"]) == 0
        swapped = capsys.readouterr().out
    assert "missed 0" in staged and "live 0" not in staged
    assert {r["model"] for r in gateway.requests} >= {"glm-5.3-flash"}
    assert "same comments" in swapped
    info = rp.read_run(tmp_path / "b/trace")
    assert info["job"]["models"]["roles"] == {"verifier_a": "glm-5.3-flash"}


def test_model_change_needs_a_live_gateway(s2_run, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HABODEK_LLM_BASE_URL", raising=False)
    assert cli.main(["replay", str(s2_run[0]), "--model", "judge=x", "--out",
                     str(tmp_path / "x")]) == 2


@pytest.mark.skipif(not local_available(), reason="no user+net namespaces here")
def test_replay_serves_sandbox_runs_from_the_recording(built, tmp_path, monkeypatch, capsys):
    run_dir, n = record(built, tmp_path / "rec", "2", monkeypatch, sandbox="local")
    original = Result.model_validate_json((run_dir / "result.json").read_text())
    witnessed = [w for f in original.findings for w in f.witnesses]
    assert witnessed and (run_dir / "runs/run1.txt").is_file()
    sandbox = rp.RecordedSandbox(run_dir)  # no fallback: everything must be recorded
    first = witnessed[0]
    got = sandbox.run(rp.RunRequest(first.root, first.cmd))
    assert got.exit_code == first.exit_code and "test_declined_cancels" in got.output \
        or first.root == "base"
    monkeypatch.delenv("HABODEK_LLM_BASE_URL", raising=False)
    assert cli.main(["replay", str(run_dir), "--out", str(tmp_path / "r")]) == 0
    assert "missed 0" in capsys.readouterr().out
    replayed = Result.model_validate_json((tmp_path / "r/trace/result.json").read_text())
    assert [w.exit_code for f in replayed.findings for w in f.witnesses] == \
        [w.exit_code for w in witnessed]


# --------------------------------------------------------------------------- probe


def test_probe_investigates_a_stated_hypothesis(s2_run, built, tmp_path, capsys, monkeypatch):
    run_dir, _ = s2_run
    gateway = fg.FakeGateway(ScriptedModel(built), validator=validator)
    with gateway.serve() as url:
        monkeypatch.setenv("HABODEK_LLM_BASE_URL", url)
        monkeypatch.setenv("HABODEK_RPM", "6000")
        code = cli.main(["probe", str(run_dir), "--hypothesis",
                         "payments-lib 1.5.0 returns declined charges instead of raising "
                         "PaymentDeclined, so checkout marks declined orders paid",
                         "--changed-line", "requirements.txt::payments-lib==1.5.0",
                         "--verify", "--out", str(tmp_path / "p")])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "confirmed:" in out and "orders/domain/checkout.py" in out
    assert "verifier_a upheld, verifier_b upheld -> would post" in out
    assert "originally: H" in out and "posted" in out
    assert len(out.strip().splitlines()) <= 20
    roles = [r["messages"][0]["content"].splitlines()[0] for r in gateway.requests]
    assert "Role: investigator" in roles and not any("finder" in r for r in roles)


def test_probe_says_when_the_finders_missed_it(s2_run):
    line = rp.original_outcome(s2_run[0], "the CSV importer leaks file handles on errors")
    assert line.startswith("originally: not hypothesised")
