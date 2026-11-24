"""Memory (M10): the memory repository, lessons, team rules, recall."""

from __future__ import annotations

import json
import subprocess

from habodek.brain.contract import Citation, EvidenceLink, LessonEvent, MemoryUpdate
from habodek.brain.memory import store
from habodek.brain.memory.context import memory_section
from habodek.brain.memory.recall import BM25, chunks, corpus, recall_tool
from habodek.brain.memory.rules import team_rules
from habodek.brain.memory.store import MemoryView, apply_updates, parse_lesson

P = "scenarios/orders-service"


def lesson(text, mr="scenarios/orders-service!8", fid="f1", scope="orders/api"):
    return MemoryUpdate(kind="lesson", project=P, lesson=LessonEvent(
        text=text, scope=scope, citation=Citation(
            mr=mr, finding_id=fid, verdict="withdrawn", head_sha="abc1234567890",
            convinced_by=[EvidenceLink(path="orders/app.py", line=31,
                                       quote='router.use("/api/v1/refunds", owns_refund)',
                                       claim="the middleware")])))


OWNS = ("Refund routes under /api/v1/refunds are guarded by the owns_refund middleware "
        "registered in orders/app.py")


def test_apply_commits_and_views_are_pinned_to_a_commit(tmp_path):
    repo = tmp_path / "mem"
    first = apply_updates(repo, [MemoryUpdate(kind="card", project=P, content="# card v1\n")],
                          "card v1")
    second = apply_updates(repo, [MemoryUpdate(kind="card", project=P, content="# card v2\n")],
                           "card v2")
    assert first != second
    assert MemoryView(repo, P, first).card() == "# card v1\n"
    assert MemoryView(repo, P).card() == "# card v2\n" and MemoryView(repo, P).commit == second
    assert apply_updates(repo, [MemoryUpdate(kind="card", project=P, content="# card v2\n")],
                         "same") == second  # nothing changed, no commit
    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"], capture_output=True,
                         text=True).stdout.split("\n")
    assert log[:3] == ["card v2", "card v1", "Start habodek-memory"]


def test_team_notes_survive_a_rewrite(tmp_path):
    repo = tmp_path / "mem"
    card = (f"# card\n\n## Team notes\n\n{store.TEAM_NOTES_START}\n(none yet)\n"
            f"{store.TEAM_NOTES_END}\n")
    apply_updates(repo, [MemoryUpdate(kind="card", project=P, content=card)], "c1")
    path = repo / "projects" / P / "card.md"
    path.write_text(path.read_text().replace("(none yet)", "Payments are idempotent by key."))
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "human edit", "-c",
                    "user.name=x"], capture_output=True)
    apply_updates(repo, [MemoryUpdate(kind="card", project=P, content=card.replace(
        "# card", "# card, remapped"))], "c2")
    text = path.read_text()
    assert "remapped" in text and "Payments are idempotent by key." in text


def test_lessons_need_two_independent_citations_to_become_active(tmp_path):
    repo = tmp_path / "mem"
    apply_updates(repo, [lesson(OWNS)], "l1")
    (first,) = MemoryView(repo, P).lessons()
    assert first.status == "candidate" and first.count == 1 and first.scope == "orders/api"
    assert MemoryView(repo, P).active_lessons() == []
    # the same thread again does not count twice
    apply_updates(repo, [lesson(OWNS)], "l1 again")
    assert MemoryView(repo, P).lessons()[0].count == 1
    # a second, independent thread with similar words merges and activates
    apply_updates(repo, [lesson("Refund routes under /api/v1/refunds are guarded by "
                                "owns_refund middleware in orders/app.py", mr=f"{P}!12",
                                fid="f9")], "l2")
    (merged,) = MemoryView(repo, P).lessons()
    assert merged.count == 2 and merged.status == "active" and len(merged.citations) == 2
    assert "`orders/app.py:31`" in merged.citations[0]
    # an unrelated lesson stays separate
    apply_updates(repo, [lesson("Import jobs run in a single worker, so no locking is needed "
                                "around the CSV reader", fid="f3")], "l3")
    assert len(MemoryView(repo, P).lessons()) == 2


def test_a_person_can_retire_a_lesson_and_the_merge_keeps_it_retired(tmp_path):
    repo = tmp_path / "mem"
    apply_updates(repo, [lesson(OWNS)], "l1")
    (path,) = (repo / "projects" / P / "lessons").glob("*.md")
    path.write_text(path.read_text().replace("status: candidate", "status: retired"))
    apply_updates(repo, [lesson(OWNS, mr=f"{P}!12", fid="f9")], "l2")
    (x,) = MemoryView(repo, P).lessons()
    assert x.status == "retired" and x.count == 2
    assert MemoryView(repo, P).active_lessons() == []


def test_active_lessons_are_scoped_to_the_changed_paths(tmp_path):
    repo = tmp_path / "mem"
    apply_updates(repo, [lesson(OWNS), lesson(OWNS, mr=f"{P}!12", fid="f9")], "l")
    view = MemoryView(repo, P)
    assert view.active_lessons(["orders/api/refunds.py"])
    assert not view.active_lessons(["orders/jobs/digest.py"])
    section = memory_section(view, ["orders/api/refunds.py"])
    assert "owns_refund" in section and "seen 2×" in section and view.commit[:10] in section


def test_lesson_file_round_trips():
    text = ("---\nid: l-1\nstatus: active\ncount: 3\nscope: api\ncreated: 2026-01-01\n"
            "updated: 2026-01-02\n---\n\nX is guaranteed by Y.\n\n## Citations\n\n"
            "- a!1 <!-- cite:a!1#f1 -->\n")
    x = parse_lesson(text, "l-1.md")
    assert (x.status, x.count, x.scope, x.cited) == ("active", 3, "api", {"a!1#f1"})
    assert parse_lesson(x.render()).text == "X is guaranteed by Y."


def test_reverse_dependency_index_from_facts(tmp_path):
    repo = tmp_path / "mem"
    facts = lambda project, pins: MemoryUpdate(  # noqa: E731
        kind="facts", project=project, content=json.dumps({"project": project, "pins": pins}))
    apply_updates(repo, [facts("scenarios/platform-common", {}),
                         facts("scenarios/orders-service", {"platform-common": "2.3.0",
                                                            "payments-lib": "1.4.2"}),
                         facts("scenarios/billing", {"payments_lib": "1.5.0"})], "facts")
    view = MemoryView(repo, "scenarios/platform-common")
    assert [f["project"] for f in view.consumers_of("platform-common")] == [
        "scenarios/orders-service"]
    assert {f["project"] for f in view.consumers_of("payments-lib")} == {
        "scenarios/orders-service", "scenarios/billing"}


def test_bm25_ranks_the_relevant_chunk_first():
    docs = (chunks("a.md", "# Auth\n\nAdmin routes get require_admin from AdminBlueprint.\n")
            + chunks("b.md", "# Jobs\n\nThe digest job emails every subscriber weekly.\n")
            + chunks("c.md", "# Store\n\nget_user raises NotFound for a missing user.\n"))
    hits = BM25(docs).search("who checks admin authorisation require_admin")
    assert hits[0][1].path == "a.md"
    assert BM25(docs).search("NotFound")[0][1].path == "c.md"
    assert BM25(docs).search("zzz") == []


def test_recall_tool_searches_own_memory_and_other_cards(tmp_path):
    repo = tmp_path / "mem"
    apply_updates(repo, [
        MemoryUpdate(kind="card", project=P, content="# orders\n\n## Invariants\n\nCheckout "
                     "cancels an order when PaymentDeclined is raised.\n"),
        MemoryUpdate(kind="card", project="scenarios/payments-lib",
                     content="# payments-lib\n\ncharge() returns ChargeResult.\n"),
        lesson(OWNS)], "m")
    view = MemoryView(repo, P)
    assert len(corpus(view)) >= 3
    tool = recall_tool(view)
    out = tool.run({"query": "ChargeResult charge"}, None)
    assert out.startswith("memory:projects/scenarios/payments-lib/card.md")
    assert "owns_refund" in tool.run({"query": "refund middleware"}, None)  # candidates too
    assert "cite code" in out


# --------------------------------------------------------------------------- rules


def _rules_tree(tmp_path):
    base = tmp_path / "base"
    (base / "api").mkdir(parents=True)
    (base / "jobs").mkdir()
    (base / ".habodek.md").write_text(
        "# Rules\n\nEvery handler must check ownership of the object it loads.\n"
        "Never ignore errors from the payment client.\n"
        "Do not flag missing type hints.\n"
        "```habodek-sandbox\ntest: pytest\n```\n")
    (base / "api" / ".habodek.md").write_text("API responses never include emails.\n"
                                              "Ignore the legacy v1 routes.\n")
    (base / "jobs" / ".habodek.md").write_text("Jobs must be idempotent.\n")
    return base


def test_rules_are_scoped_by_directory(tmp_path):
    base = _rules_tree(tmp_path)
    rules = team_rules(base, ["api/refunds.py"])
    assert rules.files == [".habodek.md", "api/.habodek.md"]
    assert "never include emails" in rules.text and "idempotent" not in rules.text
    assert "habodek-sandbox" not in rules.text and "test: pytest" not in rules.text


def test_rules_cannot_suppress(tmp_path):
    rules = team_rules(_rules_tree(tmp_path), ["api/refunds.py"])
    assert "Never ignore errors from the payment client." in rules.text  # adds a check
    assert "missing type hints" not in rules.text and "legacy v1" not in rules.text
    assert len(rules.dropped) == 2 and "not remove them" in rules.render()


def test_rules_come_from_the_target_branch_only(tmp_path):
    assert team_rules(None, ["a.py"]).text == ""
