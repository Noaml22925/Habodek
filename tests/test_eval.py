"""Tests for the evaluation harness.

The harness is the instrument every later decision is read off -- including
whether to build Habodek at all or adopt PR-Agent. A miscalibrated instrument is
worse than none, so the matching rules get tested directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.build_corpus import build
from eval.case import Case, ExpectedFinding, discover_cases, materialise
from eval.score import EvalFinding, from_pr_agent, matches, score_all, score_case
from habodek.workspace.diff import build_change_map

CORPUS = Path(__file__).parent.parent / "eval" / "corpus" / "synthetic"


def expected(**overrides) -> ExpectedFinding:
    base = {
        "id": "x",
        "category": "correctness",
        "severity": "major",
        "file": "src/a.py",
        "lines": [10, 12],
        "summary": "s",
        "keywords": ["swallow", "except"],
    }
    return ExpectedFinding(**(base | overrides))


def reported(**overrides) -> EvalFinding:
    base = {
        "file": "src/a.py",
        "line_start": 11,
        "line_end": 11,
        "title": "Exception is swallowed here",
        "body": "",
    }
    return EvalFinding(**(base | overrides))


# ---- matching ----------------------------------------------------------


def test_right_place_and_right_reason_is_a_hit() -> None:
    assert matches(expected(), reported())


def test_right_place_wrong_reason_is_not_a_hit() -> None:
    # A comment landing on the correct line while talking about something else has
    # not found the bug. Counting it would flatter every reviewer.
    assert not matches(expected(), reported(title="Consider renaming this variable"))


def test_wrong_file_is_not_a_hit() -> None:
    assert not matches(expected(), reported(file="src/other.py"))


def test_line_windows_only_need_to_overlap() -> None:
    # Reviewers reasonably disagree about whether to flag the `try` or the `except`,
    # so overlap is enough.
    assert matches(expected(lines=[10, 12]), reported(line_start=12, line_end=20))
    assert matches(expected(lines=[10, 12]), reported(line_start=5, line_end=10))
    assert not matches(expected(lines=[10, 12]), reported(line_start=13, line_end=20))


def test_keywords_are_case_insensitive_and_match_the_body() -> None:
    assert matches(expected(), reported(title="Problem", body="The error is SWALLOWED"))


def test_expectation_without_keywords_matches_on_location_alone() -> None:
    assert matches(expected(keywords=[]), reported(title="anything at all"))


def test_posix_and_windows_paths_compare_equal() -> None:
    assert matches(expected(file="src/a.py"), reported(file="src\\a.py"))


# ---- scoring -----------------------------------------------------------


def test_one_finding_cannot_claim_two_planted_flaws() -> None:
    # Otherwise a single vague comment scores as having found everything.
    case = Case(
        id="c",
        title="t",
        expect=[expected(id="first"), expected(id="second")],
    )
    result = score_case(case, [reported()])
    assert [h[0] for h in result.hits] == ["first"]
    assert [m.id for m in result.missed] == ["second"]


def test_unmatched_findings_are_counted_as_noise() -> None:
    case = Case(id="c", title="t", expect=[expected()])
    result = score_case(case, [reported(), reported(line_start=90, line_end=90, title="nit")])
    assert len(result.hits) == 1
    assert len(result.unmatched) == 1
    assert not result.perfect


def test_silence_on_a_clean_case_is_perfect() -> None:
    case = Case(id="clean", title="t", expect=[])
    result = score_case(case, [])
    assert result.is_clean
    assert result.perfect
    assert result.reported == 0


def test_any_finding_on_a_clean_case_is_noise() -> None:
    case = Case(id="clean", title="t", expect=[])
    result = score_case(case, [reported()])
    assert len(result.unmatched) == 1
    assert not result.perfect


def test_report_metrics_and_the_flag_everything_failure_mode() -> None:
    flawed = Case(id="flawed", title="t", expect=[expected()])
    clean = Case(id="clean", title="t", expect=[])

    # A reviewer that finds the bug and says nothing else.
    good = score_all({c.id: c for c in [flawed, clean]}.values(), {"flawed": [reported()]})
    assert good.detection == 1.0
    assert good.precision == 1.0
    assert good.clean_case_noise == 0

    # A reviewer that flags everything everywhere: perfect detection, useless.
    spam = [reported()] + [reported(line_start=n, line_end=n, title="nit") for n in range(50, 60)]
    loud = score_all(
        [flawed, clean],
        {"flawed": spam, "clean": [reported(title="nit")]},
    )
    assert loud.detection == 1.0  # detection alone cannot distinguish them
    assert loud.precision < 0.1  # precision can
    assert loud.clean_case_noise == 1


# ---- adapters ----------------------------------------------------------


def test_pr_agent_improve_and_review_shapes_are_both_read() -> None:
    payload = {
        "code_suggestions": [
            {
                "relevant_file": "src/payments.py",
                "relevant_lines_start": 12,
                "relevant_lines_end": 14,
                "suggestion_content": "Avoid swallowing the exception",
                "label": "possible bug",
            }
        ],
        "review": {
            "key_issues_to_review": [
                {
                    "relevant_file": "src/orders.py",
                    "start_line": 8,
                    "end_line": 8,
                    "issue_header": "SQL injection",
                    "issue_content": "status is interpolated into the query",
                }
            ]
        },
    }
    findings = from_pr_agent(payload)
    assert [f.file for f in findings] == ["src/payments.py", "src/orders.py"]
    assert findings[0].line_end == 14
    assert "swallow" in findings[0].text
    assert all(f.source == "pr-agent" for f in findings)


def test_pr_agent_payload_missing_sections_is_not_an_error() -> None:
    assert from_pr_agent({}) == []
    assert from_pr_agent({"code_suggestions": None, "review": None}) == []


# ---- the corpus itself -------------------------------------------------


def test_corpus_builds_and_has_clean_cases() -> None:
    build()
    cases = discover_cases(CORPUS)
    assert len(cases) >= 10
    # Clean cases carry most of the signal; the corpus is broken without them.
    assert sum(1 for c in cases if c.is_clean) >= 2
    categories = {e.category for c in cases for e in c.expect}
    assert {"correctness", "security", "design", "smell", "typo", "perf"} <= categories


def test_every_expectation_points_at_a_real_line(tmp_path) -> None:
    """The planted flaw must sit on a line the diff actually contains.

    If an expectation is anchored outside the diff, no reviewer could ever be
    credited for finding it and the corpus would silently under-report everyone.
    """
    build()
    for case_source in sorted(CORPUS.iterdir()):
        if not case_source.is_dir():
            continue
        found = materialise(case_source, tmp_path)
        change_map = build_change_map(found.repo, found.base, found.head)

        for expectation in found.case.expect:
            file_change = change_map.by_path(expectation.file)
            assert file_change is not None, f"{found.case.id}: {expectation.file} not in diff"
            anchors = {line for side, line in file_change.anchorable() if side == "new"}
            overlap = anchors & set(expectation.line_range)
            assert overlap, (
                f"{found.case.id}/{expectation.id}: lines {expectation.lines} "
                f"are not in the diff of {expectation.file}"
            )


def test_materialise_produces_a_two_commit_repo(tmp_path) -> None:
    build()
    found = materialise(CORPUS / "swallowed-exception", tmp_path)
    change_map = build_change_map(found.repo, found.base, found.head)

    assert found.base != found.head
    assert [f.path for f in change_map.files] == ["src/payments.py"]
    assert change_map.files[0].status == "modified"


def test_clean_case_with_multiple_files_diffs_both(tmp_path) -> None:
    build()
    found = materialise(CORPUS / "clean-guard-clause", tmp_path)
    change_map = build_change_map(found.repo, found.base, found.head)
    assert sorted(f.path for f in change_map.files) == ["src/cart.py", "tests/test_cart.py"]


@pytest.mark.parametrize(
    ("case_id", "needle"),
    [
        ("lsp-violation", "class ReadOnlyAccount(Account):"),
        ("sql-injection", "SELECT * FROM orders WHERE status"),
        ("mutable-default-argument", "events: list[str] = []"),
    ],
)
def test_expected_line_lands_on_the_intended_code(case_id: str, needle: str, tmp_path) -> None:
    build()
    found = materialise(CORPUS / case_id, tmp_path)
    change_map = build_change_map(found.repo, found.base, found.head)
    expectation = found.case.expect[0]
    file_change = change_map.by_path(expectation.file)
    assert file_change is not None

    texts = [file_change.line_text("new", n) or "" for n in expectation.line_range]
    assert any(needle in t for t in texts), f"{case_id}: expected line does not contain {needle!r}"
