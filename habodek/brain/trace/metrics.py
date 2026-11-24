"""Per-run metrics from the findings' life records (design §6.9).

Every hypothesis ends as a finding record with a status and, if dropped, the stage
that dropped it, so the funnel per lens is a count over those records: raised →
investigated → confirmed → survived checks → upheld → posted. Written to
`metrics.json` in the trace and shown by `trace show`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from habodek.brain.contract import Finding

FUNNEL = ("raised", "investigated", "confirmed", "checked", "upheld", "posted")


def primary_category(f: Finding) -> str:
    return (f.category or "uncategorised").split(",")[0].strip() or "uncategorised"


def stage_reached(f: Finding) -> int:
    """How far down the funnel a finding record got (index into FUNNEL)."""
    if f.status == "posted":
        return 5
    if f.status == "not_investigated":
        return 0
    stage = f.dropped_at or ""
    if stage == "investigate":
        return 1
    if stage.startswith("check"):
        return 2
    if stage in ("verify_a", "verify_b"):
        return 3
    return 4  # judge, bar, budget at posting


def run_metrics(findings: list[Finding]) -> dict[str, Any]:
    by_lens: dict[str, Counter] = defaultdict(Counter)
    drops: Counter = Counter()
    for f in findings:
        if f.dropped_at == "check:merge":  # a duplicate, not a separate outcome
            continue
        reached = stage_reached(f)
        lens = by_lens[primary_category(f)]
        for i in range(reached + 1):
            lens[FUNNEL[i]] += 1
        if f.status == "dropped":
            drops[f.dropped_at or "?"] += 1
    total: Counter = Counter()
    for c in by_lens.values():
        total.update(c)
    return {"funnel": {k: total[k] for k in FUNNEL},
            "by_lens": {lens: {k: c[k] for k in FUNNEL} for lens, c in sorted(by_lens.items())},
            "dropped_at": dict(sorted(drops.items()))}
