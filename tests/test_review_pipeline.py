"""The review workflow end to end on the scenarios, with scripted models (M6).

Good model: S1 and S2 produce one comment each, with the expected chain and anchor;
S3 and S4 stay silent. Bad models (invented quote, anchor off the diff, no evidence,
a verifier that refutes) post nothing, and every drop is recorded with its stage.
"""

from __future__ import annotations

import json

import chat_template as ct
import pytest
from scripted_model import ScriptedModel

from eval.scenarios import build as scenario_build
from eval.scenarios.prepare import prepare
from eval.scenarios.score import score_review
from habodek.brain.codeintel.refs import SymbolIndex
from habodek.brain.contract import Anchor, EvidenceLink, Finding
from habodek.brain.llm import fake_gateway as fg
from habodek.brain.llm.client import LLMClient
from habodek.brain.llm.profiles import load_profiles
from habodek.brain.trace.writer import TraceWriter
from habodek.brain.workflow.pipeline import Review, ReviewConfig, run_review

PROFILES = load_profiles(env={})


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return scenario_build.build(tmp_path_factory.mktemp("sc") / "build")


def validator(model, body):
    ct.render(PROFILES.get(model).name, body)


def review(built, sid, tmp_path, mode="good", config=None, runs_for=None):
    model = ScriptedModel(built, mode)
    gateway = fg.FakeGateway(model, validator=validator)
    trace = TraceWriter(tmp_path / f"trace-{sid}-{mode}")
    client = LLMClient("http://g.invalid/v1", "k", profiles=PROFILES, trace=trace,
                       transport=gateway.transport(), sleep=lambda s: None)
    job, ws = prepare(built, sid, tmp_path / "work", f"{mode}")
    runs = runs_for(ws, trace) if runs_for else None
    result = run_review(job, ws, client, trace, config or ReviewConfig(parallel=3), runs=runs)
    return result, model, trace, gateway


def expected(built, sid):
    return json.loads((built / "expected" / f"{sid}.json").read_text())


@pytest.mark.parametrize("sid", ["1", "2"])
def test_good_model_finds_the_cross_repo_bugs(built, tmp_path, sid):
    result, model, trace, _ = review(built, sid, tmp_path)
    assert result.status == "ok", result.reason
    score = score_review(sid, expected(built, sid), result)
    assert score.passed, (score, [f.drop_reason for f in result.findings])
    (comment,) = result.comments
    assert comment.marker in comment.body and "upheld by two independent verifiers" in comment.body
    assert result.requests <= 300
    roles = set(model.calls)
    assert {"understand", "investigator", "verifier", "judge"} <= roles
    assert sum(r.startswith("finder (") for r in roles) >= 6
    assert (trace.dir / "findings.json").is_file()
    assert (trace.dir / "evidence_pack.md").read_text().count("## ") >= 4


def test_s1_comment_cites_every_site(built, tmp_path):
    result, *_ = review(built, "1", tmp_path)
    body = result.comments[0].body
    assert "orders-service `orders/api/profile.py:17`" in body
    assert "orders-service `orders/jobs/digest.py:22`" in body
    assert "`platform_common/cache/users.py:27`" in body


def test_s2_evidence_includes_the_library_diff(built, tmp_path):
    _, _, trace, _ = review(built, "2", tmp_path)
    pack = (trace.dir / "evidence_pack.md").read_text()
    assert "Library diff: payments-lib 1.4.2 -> 1.5.0" in pack
    assert '+            return ChargeResult(response.get("id", ""), "declined"' in pack


@pytest.mark.parametrize("sid", ["3", "4"])
def test_good_model_is_silent_on_clean_mrs(built, tmp_path, sid):
    result, *_ = review(built, sid, tmp_path)
    score = score_review(sid, expected(built, sid), result)
    assert score.passed and result.comments == [], result.summary
    (dropped,) = [f for f in result.findings if f.status == "dropped"]
    assert dropped.dropped_at == "investigate" and dropped.drop_reason.startswith("refuted")
    assert "No issues found." in result.summary and "Hypotheses investigated: 1" in result.summary
    assert "Checked:" in result.summary


@pytest.mark.parametrize("mode,needle", [
    ("invent_quote", "quotes are not in the code"),
    ("off_diff", "not an added or removed line"),
    ("no_evidence", "at least two links"),
])
def test_bad_model_is_stopped_by_the_checks(built, tmp_path, mode, needle):
    result, _, trace, gateway = review(built, "1", tmp_path, mode)
    assert result.comments == []
    dropped = [f for f in result.findings if f.status == "dropped"]
    assert dropped and all(f.dropped_at for f in dropped)
    assert any(f.dropped_at == "investigate" and "malformed" in f.drop_reason for f in dropped)
    errors = [e for e in trace.events() if e["kind"] == "tool_error" and e["tool"] == "conclude"]
    assert errors and needle in errors[0]["error"]


def test_verifier_refutation_fails_closed(built, tmp_path):
    result, *_ = review(built, "1", tmp_path, "verifier_refutes")
    assert result.comments == []
    (finding,) = [f for f in result.findings if f.dropped_at == "verify_a"]
    assert finding.drop_reason.startswith("refuted")
    assert finding.verifications[0].evidence  # the refutation's quote was verified


def test_budget_leftovers_are_recorded_not_dropped(built, tmp_path):
    config = ReviewConfig(parallel=2, max_investigations=0)
    result, *_ = review(built, "1", tmp_path, config=config)
    assert result.comments == []
    assert result.not_investigated
    (left,) = [f for f in result.findings if f.status == "not_investigated"]
    assert left.drop_reason == "not investigated: budget"
    assert "Not investigated (request budget): 1" in result.summary
    score = score_review("1", expected(built, "1"), result)
    assert score.lost_at == "not investigated: budget"


def test_post_hoc_checks(built, tmp_path):
    job, ws = prepare(built, "1", tmp_path / "w", "checks")
    idle = LLMClient("http://g.invalid/v1", "k", profiles=PROFILES,
                     transport=fg.FakeGateway([]).transport())
    review_ = Review(job, ws, idle, TraceWriter(tmp_path / "t"),
                     index=SymbolIndex(ws, use_jedi=False))
    good_chain = [EvidenceLink(path="platform_common/cache/users.py", line=20,
                               quote="except NotFound:", claim="c")]

    def finding(fid, snippet, chain):
        return Finding(id=fid, title=f"t{fid}", claim="c",
                       anchor=Anchor(path="platform_common/store/users.py", line=0,
                                     snippet=snippet), chain=chain)

    kept = review_.check([
        finding("A", "return None", good_chain),
        finding("B", "return None", [EvidenceLink(path="platform_common/cache/users.py",
                                                  line=20, quote="user = maybe_none()",
                                                  claim="invented")]),
        finding("C", "row = _TABLE.get(uid)", good_chain),  # context line, not a change
        finding("D", "return None", [*good_chain]),
    ])
    assert len(kept) == 1 and kept[0].anchor.line == 28
    assert kept[0].chain[1].line == 27  # the line was corrected from the quote
    stages = {f.id: (f.dropped_at, f.drop_reason) for f in review_.findings}
    assert stages["B"][0] == "check:evidence" and "not in the file" in stages["B"][1]
    assert stages["C"][0] == "check:anchor"
    assert [s for s, _ in stages.values()].count("check:merge") == 1


def test_scorecard_cli_end_to_end_over_http(built, tmp_path, monkeypatch, capsys):
    """The command the owner runs on the VPN, against the fake gateway over HTTP."""
    from habodek.brain import cli

    gateway = fg.FakeGateway(ScriptedModel(built), validator=validator)
    with gateway.serve() as url:
        monkeypatch.setenv("HABODEK_LLM_BASE_URL", url)
        monkeypatch.setenv("HABODEK_LLM_TOKEN", "k")
        monkeypatch.setenv("HABODEK_RPM", "6000")
        monkeypatch.setenv("HABODEK_HOME", str(tmp_path / "home"))
        code = cli.main(["scorecard", "--scenarios", "1-4", "--runs", "1", "--build",
                         str(built), "--out", str(tmp_path / "card")])
    lines = capsys.readouterr().out.strip().splitlines()
    assert code == 0 and len(lines) <= 20
    assert lines[0].startswith("habodek scorecard") and "inv=glm-5.3-flash" in lines[0]
    assert "FOUND  1/1  chain=1/1 anchor=1/1" in lines[1]
    assert "FOUND  1/1  chain=1/1 anchor=1/1" in lines[2]
    assert "SILENT 1/1" in lines[3] and "SILENT 1/1" in lines[4]
    assert "found 2/2  silent 2/2" in lines[5]
    assert (tmp_path / "card" / "s1" / "run1" / "trace" / "events.jsonl").is_file()


def test_review_cli_resolves_dependencies_itself(built, tmp_path, monkeypatch, capsys):
    """Production jobs carry no dependency source: the brain fetches it (§3.5)."""
    from habodek.brain import cli
    from habodek.brain.jobs import save_job

    job, _ = prepare(built, "2", tmp_path / "work", "cli")
    job.workspace.dependencies = []
    save_job(job, tmp_path / "job.json")
    gateway = fg.FakeGateway(ScriptedModel(built), validator=validator)
    with gateway.serve() as url:
        for key, value in {"HABODEK_LLM_BASE_URL": url, "HABODEK_RPM": "6000",
                           "HABODEK_INTERNAL_PACKAGES": "payments-lib,platform-common",
                           "HABODEK_PIP_FIND_LINKS": str(built / "index"),
                           "HABODEK_DEPS_CACHE": str(tmp_path / "cache")}.items():
            monkeypatch.setenv(key, value)
        code = cli.main(["review", "--job", str(tmp_path / "job.json"), "--out",
                         str(tmp_path / "result.json"), "--trace", str(tmp_path / "trace")])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "payments-lib==1.5.0: downloaded" in captured.err
    result = json.loads((tmp_path / "result.json").read_text())
    assert len(result["comments"]) == 1
