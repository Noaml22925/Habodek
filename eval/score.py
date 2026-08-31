"""Score a reviewer against the corpus.

Deliberately reviewer-agnostic. It grades a normalised list of findings, so the
same harness measures Habodek, PR-Agent, or a human -- which is the point: the
build-versus-buy decision needs both sides measured the same way.

Three numbers matter, in this order:

* **Noise on clean cases.** Findings raised against a change with nothing wrong.
  This is the sharpest signal in the whole harness and the one that predicts
  whether a team keeps the bot switched on. A reviewer can score well on
  everything else and still be unusable if this is not near zero.
* **Precision.** Of everything it said, how much was worth saying.
* **Detection.** Of the planted flaws, how many it found.

Detection is listed last on purpose. A reviewer that reports every possible
concern gets perfect detection and is worthless.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eval.case import Case, ExpectedFinding


@dataclass(slots=True)
class EvalFinding:
    """A reported finding, normalised across reviewers."""

    file: str
    line_start: int
    line_end: int
    title: str
    body: str = ""
    category: str = ""
    severity: str = ""
    source: str = ""

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}".lower()

    @property
    def location(self) -> str:
        if self.line_start == self.line_end:
            return f"{self.file}:{self.line_start}"
        return f"{self.file}:{self.line_start}-{self.line_end}"


# ---- adapters ----------------------------------------------------------


def from_objects(findings: list[Any]) -> list[EvalFinding]:
    """Normalise any objects exposing the finding attributes.

    Duck-typed on purpose so a reviewer's own result type can be scored without
    this module importing it.
    """
    return [
        EvalFinding(
            file=f.file,
            line_start=f.line_start,
            line_end=f.line_end,
            title=f.title,
            body=f.body,
            category=f.category,
            severity=f.severity,
            source="objects",
        )
        for f in findings
    ]


def from_pr_agent(payload: dict[str, Any]) -> list[EvalFinding]:
    """Normalise PR-Agent / Qodo Merge output.

    Handles the `/improve` suggestion shape (`code_suggestions`) and the
    `/review` shape (`review.key_issues_to_review`), since the two commands
    report differently and a fair comparison needs both.
    """
    out: list[EvalFinding] = []

    for suggestion in payload.get("code_suggestions", []) or []:
        start = int(suggestion.get("relevant_lines_start", 0) or 0)
        end = int(suggestion.get("relevant_lines_end", start) or start)
        out.append(
            EvalFinding(
                file=suggestion.get("relevant_file", "").strip(),
                line_start=start,
                line_end=max(end, start),
                title=suggestion.get("suggestion_content", "") or suggestion.get("label", ""),
                body=str(suggestion.get("improved_code", "")),
                category=suggestion.get("label", ""),
                source="pr-agent",
            )
        )

    review = payload.get("review", {}) or {}
    for issue in review.get("key_issues_to_review", []) or []:
        start = int(issue.get("start_line", 0) or 0)
        end = int(issue.get("end_line", start) or start)
        out.append(
            EvalFinding(
                file=issue.get("relevant_file", "").strip(),
                line_start=start,
                line_end=max(end, start),
                title=issue.get("issue_header", ""),
                body=issue.get("issue_content", ""),
                category=issue.get("issue_header", ""),
                source="pr-agent",
            )
        )

    return out


def from_json_file(path: Path) -> dict[str, list[EvalFinding]]:
    """Load findings keyed by case id.

    Accepts either `{case_id: [finding, ...]}` with normalised findings, or
    `{case_id: <pr-agent payload>}`.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, list[EvalFinding]] = {}
    for case_id, payload in raw.items():
        if isinstance(payload, dict):
            out[case_id] = from_pr_agent(payload)
        else:
            out[case_id] = [
                EvalFinding(
                    file=item["file"],
                    line_start=int(item.get("line_start", item.get("line", 0))),
                    line_end=int(item.get("line_end", item.get("line", 0))),
                    title=item.get("title", ""),
                    body=item.get("body", ""),
                    category=item.get("category", ""),
                    severity=item.get("severity", ""),
                    source=item.get("source", "external"),
                )
                for item in payload
            ]
    return out


# ---- matching ----------------------------------------------------------


def matches(expected: ExpectedFinding, finding: EvalFinding) -> bool:
    """Whether a reported finding counts as having found a planted flaw.

    Requires both the right place and the right reason. Location alone is not
    enough -- a comment landing on the correct line while talking about something
    else has not found the bug, and counting it would flatter every reviewer.
    """
    if Path(finding.file).as_posix() != Path(expected.file).as_posix():
        return False
    if not expected.covers(finding.line_start, finding.line_end):
        return False
    if not expected.keywords:
        return True
    return any(keyword.lower() in finding.text for keyword in expected.keywords)


@dataclass
class CaseScore:
    case_id: str
    is_clean: bool
    hits: list[tuple[str, EvalFinding]] = field(default_factory=list)
    missed: list[ExpectedFinding] = field(default_factory=list)
    unmatched: list[EvalFinding] = field(default_factory=list)

    @property
    def planted(self) -> int:
        return len(self.hits) + len(self.missed)

    @property
    def reported(self) -> int:
        return len(self.hits) + len(self.unmatched)

    @property
    def perfect(self) -> bool:
        return not self.missed and not self.unmatched


def score_case(case: Case, findings: list[EvalFinding]) -> CaseScore:
    result = CaseScore(case_id=case.id, is_clean=case.is_clean)
    claimed: set[int] = set()

    for expected in case.expect:
        for index, finding in enumerate(findings):
            if index in claimed or not matches(expected, finding):
                continue
            result.hits.append((expected.id, finding))
            claimed.add(index)
            break
        else:
            result.missed.append(expected)

    result.unmatched = [f for i, f in enumerate(findings) if i not in claimed]
    return result


@dataclass
class Report:
    scores: list[CaseScore] = field(default_factory=list)

    @property
    def planted(self) -> int:
        return sum(s.planted for s in self.scores)

    @property
    def found(self) -> int:
        return sum(len(s.hits) for s in self.scores)

    @property
    def reported(self) -> int:
        return sum(s.reported for s in self.scores)

    @property
    def noise(self) -> int:
        return sum(len(s.unmatched) for s in self.scores)

    @property
    def clean_case_noise(self) -> int:
        """Findings raised against changes with nothing wrong. The key metric."""
        return sum(len(s.unmatched) for s in self.scores if s.is_clean)

    @property
    def clean_cases(self) -> int:
        return sum(1 for s in self.scores if s.is_clean)

    @property
    def detection(self) -> float:
        return self.found / self.planted if self.planted else 0.0

    @property
    def precision(self) -> float:
        return self.found / self.reported if self.reported else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.detection
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "planted": self.planted,
            "found": self.found,
            "reported": self.reported,
            "noise": self.noise,
            "clean_case_noise": self.clean_case_noise,
            "detection": round(self.detection, 3),
            "precision": round(self.precision, 3),
            "f1": round(self.f1, 3),
            "cases": {
                s.case_id: {
                    "clean": s.is_clean,
                    "hits": [h[0] for h in s.hits],
                    "missed": [m.id for m in s.missed],
                    "unmatched": [f.location for f in s.unmatched],
                }
                for s in self.scores
            },
        }


def score_all(cases: list[Case], findings_by_case: dict[str, list[EvalFinding]]) -> Report:
    return Report(scores=[score_case(c, findings_by_case.get(c.id, [])) for c in cases])
