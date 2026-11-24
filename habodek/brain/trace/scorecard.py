"""`habodek-brain scorecard`: run scenarios N times, score them, print ≤ 20 lines.

    habodek-brain scorecard --scenarios 1-4 --runs 3 [--out DIR] [--build DIR]

Each line is diagnostic on its own; for a miss it says which stage lost it. Full
traces, results and a side-by-side with the Claude Code reference reviews go to the
output directory, printed last (handoff §5).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from habodek.brain import __version__
from habodek.brain.contract import Result, ReviewJob, ThreadJob

Runner = Callable[[ReviewJob | ThreadJob, object, Path], Result]


def _scenario_dir() -> Path:
    import eval.scenarios

    return Path(eval.scenarios.__file__).parent


@dataclass
class ScenarioRuns:
    sid: str
    name: str
    kind: str
    scores: list = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _fmt_minutes(seconds: list[float]) -> str:
    if not seconds:
        return "-"
    avg = sum(seconds) / len(seconds)
    return f"{avg / 60:.0f}m" if avg >= 60 else f"{avg:.0f}s"


def review_line(runs: ScenarioRuns) -> str:
    scores = runs.scores
    n = len(scores)
    label = f"S{runs.sid} {runs.name:<15}"[:19]
    req = round(sum(s.requests for s in scores) / n) if n else 0
    if not scores:
        return f"{label} ERROR  {'; '.join(runs.errors)[:70]}"
    if scores[0].expected == "silent":
        silent = sum(s.outcome == "SILENT" for s in scores)
        status = "SILENT" if silent == n else "NOISY"
        extra = next((s.lost_at for s in scores if s.outcome == "NOISY"), "")
        hyps = round(sum(s.hypotheses for s in scores) / n)
        refuted = round(sum(s.refuted for s in scores) / n)
        posted = ",".join(str(s.posted) for s in scores)
        text = (f"{label} {status:<6} {silent}/{n}  posted={posted}"
                f"  hyps={hyps} refuted={refuted}  req={req} {_fmt_minutes(runs.seconds)}")
        return text + (f"  {extra[:60]}" if extra else "")
    found = sum(s.outcome == "FOUND" for s in scores)
    status = "FOUND" if found == n else ("MISS" if found == 0 else "PART")
    chain = sum(s.chain_ok for s in scores)
    anchor = sum(s.anchor_ok for s in scores)
    text = (f"{label} {status:<6} {found}/{n}  chain={chain}/{n} anchor={anchor}/{n}"
            f"  posted={','.join(str(s.posted) for s in scores)}  req={req} "
            f"{_fmt_minutes(runs.seconds)}")
    miss = next((s.lost_at for s in scores if s.outcome == "MISS"), "")
    gap = next((",".join(s.missing_sites) for s in scores if s.missing_sites), "")
    if miss:
        text += f"  miss: {miss[:60]}"
    elif gap:
        text += f"  chain lacks: {gap[:60]}"
    return text


def thread_line(runs: ScenarioRuns) -> str:
    scores = runs.scores
    n = len(scores)
    label = f"S{runs.sid} {runs.name:<15}"[:19]
    if not scores:
        return f"{label} ERROR  {'; '.join(runs.errors)[:70]}"
    ok = sum(s.passed for s in scores)
    status = "OK" if ok == n else ("WRONG" if ok == 0 else "PART")
    got = ",".join(s.got for s in scores)
    req = round(sum(s.requests for s in scores) / n)
    text = (f"{label} {status:<6} {ok}/{n}  want={scores[0].expected} got={got}  req={req} "
            f"{_fmt_minutes(runs.seconds)}")
    gap = next((",".join(s.missing_sites) for s in scores if s.missing_sites), "")
    if gap:
        text += f"  not cited: {gap[:50]}"
    if not all(s.lesson_ok for s in scores):
        text += "  no lesson"
    return text


def run_scorecard(ids: list[str], runs: int, out_dir: Path, build_dir: Path,
                  runner: Runner, models_line: str = "") -> list[str]:
    from eval.scenarios import specs
    from eval.scenarios.prepare import prepare
    from eval.scenarios.score import score_review, score_thread

    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[ScenarioRuns] = []
    totals = {"malformed": 0, "degenerate": 0, "throttled": 0, "requests": 0}
    started = time.time()
    for sid in ids:
        scenario = specs.SCENARIOS[sid]
        expect = json.loads((build_dir / "expected" / f"{sid}.json").read_text())
        entry = ScenarioRuns(sid, scenario["name"], scenario["kind"])
        for run in range(1, runs + 1):
            label = f"run{run}"
            run_dir = out_dir / f"s{sid}" / label
            try:
                job, ws = prepare(build_dir, sid, out_dir / "work", label)
                t0 = time.time()
                result = runner(job, ws, run_dir)
                entry.seconds.append(time.time() - t0)
            except Exception as exc:  # noqa: BLE001 - one broken run must not end the card
                entry.errors.append(f"{label}: {type(exc).__name__}: {str(exc)[:80]}")
                continue
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "result.json").write_text(result.model_dump_json(indent=2))
            llm = result.llm or {}
            totals["malformed"] += int(llm.get("malformed_calls", 0))
            totals["degenerate"] += sum((llm.get("degenerate") or {}).values())
            totals["throttled"] += int(llm.get("throttled", 0))
            totals["requests"] += result.requests
            if scenario["kind"] == "review":
                entry.scores.append(score_review(sid, expect, result))
            else:
                entry.scores.append(score_thread(sid, expect, result))
        results.append(entry)
        _side_by_side(out_dir, sid, entry)

    elapsed_min = max((time.time() - started) / 60, 1e-9)
    lines = [f"habodek scorecard {time.strftime('%Y-%m-%d')}  brain {__version__}  "
             f"{models_line}".rstrip()]
    for entry in results:
        lines.append(review_line(entry) if entry.kind == "review" else thread_line(entry))
    reviews = [s for e in results if e.kind == "review" for s in e.scores]
    found = [s for s in reviews if s.expected == "found"]
    silent = [s for s in reviews if s.expected == "silent"]
    threads = [s for e in results if e.kind == "thread" for s in e.scores]
    summary = f"runs {runs}/{runs}"
    if found:
        summary += f"  found {sum(s.outcome == 'FOUND' for s in found)}/{len(found)}"
    if silent:
        summary += f"  silent {sum(s.outcome == 'SILENT' for s in silent)}/{len(silent)}"
    if threads:
        summary += f"  verdicts {sum(s.passed for s in threads)}/{len(threads)}"
    summary += (f"  malformed={totals['malformed']}  degenerate={totals['degenerate']}  "
                f"429={totals['throttled']}  rpm_obs={totals['requests'] / elapsed_min:.1f}")
    lines.append(summary)
    errors = [e for r in results for e in r.errors]
    if errors:
        lines.append(f"errors: {len(errors)} ({errors[0][:80]})")
    lines.append(f"details: {out_dir}")
    (out_dir / "scorecard.txt").write_text("\n".join(lines) + "\n")
    return lines[:20]


def _side_by_side(out_dir: Path, sid: str, entry: ScenarioRuns) -> None:
    """Habodek's output next to the Claude Code reference review, for a human to read."""
    reference = _scenario_dir() / f"s{sid}" / "reference_claude_code.md"
    parts = [f"# Scenario S{sid} {entry.name}: Habodek vs the Claude Code reference\n"]
    parts.append("## Claude Code reference\n")
    parts.append(reference.read_text() if reference.is_file() else "(no reference)\n")
    for run_dir in sorted((out_dir / f"s{sid}").glob("run*")):
        result_file = run_dir / "result.json"
        if not result_file.is_file():
            continue
        result = Result.model_validate_json(result_file.read_text())
        parts.append(f"\n## Habodek, {run_dir.name}\n")
        for comment in result.comments:
            parts.append(f"### Comment on {comment.path}:{comment.line}\n\n{comment.body}\n")
        if result.thread:
            parts.append(f"### Thread verdict: {result.thread.verdict}\n\n{result.thread.body}\n")
        parts.append(f"### Summary\n\n{result.summary}\n")
    (out_dir / f"s{sid}-side-by-side.md").write_text("\n".join(parts))
