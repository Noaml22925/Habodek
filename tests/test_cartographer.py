"""Memory end to end (M10): the cartographer, memory in review and thread jobs, lessons.

The scripted model plays the cartographer with real tool calls on the scenario repos;
what it records is checked against the code like a finding.
"""

from __future__ import annotations

import json

import chat_template as ct
import pytest
from scripted_model import ScriptedModel

from eval.scenarios import build as scenario_build
from eval.scenarios.prepare import prepare, prepare_map
from habodek.brain import cli
from habodek.brain.contract import EvidenceLink, MemoryUpdate, Result
from habodek.brain.jobs import save_job
from habodek.brain.llm import fake_gateway as fg
from habodek.brain.llm.client import LLMClient
from habodek.brain.llm.profiles import load_profiles
from habodek.brain.memory.cartographer import run_map
from habodek.brain.memory.context import lesson_update
from habodek.brain.memory.store import MemoryView, apply_updates
from habodek.brain.thread.agent import run_thread
from habodek.brain.trace.writer import TraceWriter
from habodek.brain.workflow.pipeline import ReviewConfig, run_review

PROFILES = load_profiles(env={})
ORDERS = "scenarios/orders-service"
INTERNAL = ("platform-common", "payments-lib")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return scenario_build.build(tmp_path_factory.mktemp("carto") / "build")


def _client(model, trace):
    gateway = fg.FakeGateway(model, validator=lambda m, b: ct.render(PROFILES.get(m).name, b))
    return LLMClient("http://g.invalid/v1", "k", profiles=PROFILES, trace=trace,
                     transport=gateway.transport(), sleep=lambda s: None), gateway


def map_repo(built, tmp_path, repo, memory_dir, mode="good", label="map"):
    job, ws = prepare_map(built, repo, tmp_path / "work", label)
    view = MemoryView(memory_dir, job.mr.project) if (memory_dir / ".git").exists() else None
    trace = TraceWriter(tmp_path / f"trace-map-{repo}-{label}")
    client, gateway = _client(ScriptedModel(built, mode), trace)
    result = run_map(job, ws, client, trace, view, INTERNAL)
    return result, trace, gateway


def test_cartographer_writes_a_verified_card_and_facts(built, tmp_path):
    result, trace, gateway = map_repo(built, tmp_path, "orders-service", tmp_path / "mem")
    assert result.status == "ok" and result.kind == "map", result.reason
    assert result.requests <= 300
    card = next(u for u in result.memory_updates if u.kind == "card").content
    facts = json.loads(next(u for u in result.memory_updates if u.kind == "facts").content)
    assert facts["project"] == ORDERS and facts["package"] == "orders-service"
    assert facts["pins"] == {"payments-lib": "1.4.2", "platform-common": "2.3.0"}
    assert facts["areas"] == ["orders"]  # tests/ is not an area
    assert "owns_refund middleware" in card and "`orders/app.py:" in card
    assert "Entry points" in card and "def build_router() -> Router:" in card
    assert "Hot spots" in card and "Fix double charge on retry" in card
    assert "## Team notes" in card
    assert all(r.get("model") for r in gateway.requests)
    priorities = {e["priority"] for e in trace.events() if e["kind"] == "llm_request"}
    assert priorities == {"background"}


def test_an_unverified_invariant_never_reaches_the_card(built, tmp_path):
    result, trace, _ = map_repo(built, tmp_path, "orders-service", tmp_path / "mem",
                                mode="invent_card")
    card = result.memory_updates[0].content
    assert "transaction" not in card and "owns_refund" in card
    assert "1 unverified item(s) left out" in card
    (end,) = [e for e in trace.events() if e["kind"] == "map_end"]
    assert any("transaction" in d for d in end["dropped"])


def _apply(memory_dir, result: Result, tmp_path, name="r.json"):
    path = tmp_path / name
    path.write_text(result.model_dump_json())
    assert cli.main(["memory-apply", "--repo", str(memory_dir), "--result", str(path)]) == 0
    return MemoryView(memory_dir, result.memory_updates[0].project)


def test_memory_apply_cli_and_the_consumer_index(built, tmp_path, capsys):
    mem = tmp_path / "mem"
    orders, *_ = map_repo(built, tmp_path, "orders-service", mem)
    view = _apply(mem, orders, tmp_path)
    assert "owns_refund" in view.card() and view.facts()["sha"]
    # platform-common, mapped after its consumer: its card names the consumer
    common, *_ = map_repo(built, tmp_path, "platform-common", mem)
    card = next(u for u in common.memory_updates if u.kind == "card").content
    assert "## Consumers" in card and f"{ORDERS} pins `platform-common==2.3.0`" in card
    assert "get_user raises NotFound" in card
    view = _apply(mem, common, tmp_path, "c.json")
    assert [f["project"] for f in view.consumers_of("platform-common")] == [ORDERS]
    assert capsys.readouterr().out.strip().splitlines()[-1] == view.commit


def test_remapping_starts_from_the_previous_card(built, tmp_path):
    mem = tmp_path / "mem"
    first, *_ = map_repo(built, tmp_path, "orders-service", mem)
    _apply(mem, first, tmp_path)
    _, _, gateway = map_repo(built, tmp_path, "orders-service", mem, label="again")
    task = gateway.requests[0]["messages"][1]["content"]
    assert "# The earlier card" in task and "owns_refund" in task


# --------------------------------------------------------------------------- reviews


def _memory_with_lessons(built, tmp_path):
    mem = tmp_path / "mem"
    orders, *_ = map_repo(built, tmp_path, "orders-service", mem)
    _apply(mem, orders, tmp_path)
    from test_memory import lesson

    apply_updates(mem, [lesson("Checkout relies on PaymentDeclined to cancel declined orders",
                               scope="", fid=f) for f in ("f1", "f2")]
                  + [lesson("Checkout relies on PaymentDeclined to cancel declined orders",
                            scope="", mr=f"{ORDERS}!3", fid="f7")], "lessons")
    return mem


def test_review_sees_card_lessons_and_rules_and_records_the_commit(built, tmp_path):
    mem = _memory_with_lessons(built, tmp_path)
    job, ws = prepare(built, "2", tmp_path / "work", "memory")
    job.memory_path = str(mem)
    (ws.base / ".habodek.md").write_text("Every change to payments must keep declined orders "
                                         "cancelled.\nDo not flag dependency bumps.\n")
    view = MemoryView(mem, ORDERS)
    trace = TraceWriter(tmp_path / "trace-review")
    client, gateway = _client(ScriptedModel(built), trace)
    result = run_review(job, ws, client, trace, ReviewConfig(parallel=3), memory=view)
    assert result.status == "ok" and result.comments
    assert result.memory_commit == view.commit

    def tasks(role_prefix):
        return [r["messages"][1]["content"] for r in gateway.requests
                if r["messages"][0]["content"].startswith(f"Role: {role_prefix}")]

    finder, verifier = tasks("finder")[0], tasks("verifier")[0]
    for text in (finder, verifier, tasks("investigator")[0]):
        assert "Repository memory" in text and "owns_refund" in text  # the card
        assert "Checkout relies on PaymentDeclined" in text  # an active lesson
    assert "keep declined orders cancelled" in finder and "team's rules" in finder
    assert "Do not flag dependency bumps" not in finder  # suppression is dropped
    for role in ("verifier", "investigator", "judge"):  # rules never reach a refuter
        assert all("team's rules" not in t for t in tasks(role))
    tools = {t["function"]["name"] for r in gateway.requests for t in r.get("tools") or []}
    assert "recall" in tools
    (rules,) = [e for e in trace.events() if e["kind"] == "team_rules"]
    assert rules["files"] == [".habodek.md"] and len(rules["dropped"]) == 1


def test_withdrawn_thread_yields_a_lesson_with_its_citation(built, tmp_path):
    mem = _memory_with_lessons(built, tmp_path)
    job, ws = prepare(built, "8", tmp_path / "work", "lesson")
    job.memory_path = str(mem)
    view = MemoryView(mem, ORDERS)
    trace = TraceWriter(tmp_path / "trace-thread")
    client, _ = _client(ScriptedModel(built), trace)
    result = run_thread(job, ws, client, trace, memory=view)
    assert result.thread.verdict == "withdrawn"
    assert result.memory_commit == view.commit
    (update,) = result.memory_updates
    assert update.kind == "lesson" and update.lesson.citation.finding_id == job.finding.id
    assert update.lesson.citation.convinced_by  # the code that convinced it
    after = _apply(mem, result, tmp_path, "t.json")
    new = [x for x in after.lessons() if "Checkout" not in x.text]
    assert len(new) == 1 and new[0].status == "candidate" and new[0].count == 1


def test_only_withdrawn_verdicts_teach(built, tmp_path):
    job, _ = prepare(built, "9", tmp_path / "work", "x")
    from habodek.brain.contract import ThreadVerdict

    link = EvidenceLink(path="a.py", line=1, quote="x", claim="y")
    for verdict in ("stands", "fixed", "unclear", "fix_incomplete", "need_info"):
        assert lesson_update(job, ThreadVerdict(verdict=verdict, body="b", remaining=[link],
                                                lesson_candidate="something")) is None
    assert lesson_update(job, ThreadVerdict(verdict="withdrawn", body="b",
                                            convinced_by=[link])) is None  # no lesson text


def test_map_cli_on_a_job_file(built, tmp_path, monkeypatch, capsys):
    job, _ = prepare_map(built, "payments-lib", tmp_path / "work")
    path = tmp_path / "job.json"
    save_job(job, path)
    model = ScriptedModel(built)
    gateway = fg.FakeGateway(model)
    with gateway.serve() as url:
        monkeypatch.setenv("HABODEK_LLM_BASE_URL", url)
        monkeypatch.setenv("HABODEK_LLM_TOKEN", "t")
        rc = cli.main(["map", "--job", str(path), "--out", str(tmp_path / "out.json"),
                       "--trace", str(tmp_path / "trace")])
    assert rc == 0, capsys.readouterr()
    result = Result.model_validate_json((tmp_path / "out.json").read_text())
    assert result.kind == "map" and {u.kind for u in result.memory_updates} == {"card", "facts"}
    assert isinstance(result.memory_updates[0], MemoryUpdate)
