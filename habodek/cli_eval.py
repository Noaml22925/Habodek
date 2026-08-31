"""The `habodek eval` command.

Reviewer-agnostic by design: the same harness grades Habodek and PR-Agent, so the
build-versus-buy decision rests on measurements rather than impressions.

The metrics are printed noise-first. Detection is the number everyone reaches for
and the least useful on its own -- a reviewer that flags everything scores
perfectly on it and is worthless in practice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

console = Console()

CORPUS = Path(__file__).parent.parent / "eval" / "corpus" / "synthetic"


def register(app: typer.Typer) -> None:
    app.command("eval")(evaluate)


def evaluate(
    from_json: Annotated[
        Path | None,
        typer.Option("--from-json", help="Score an external reviewer's output (e.g. PR-Agent)."),
    ] = None,
    materialise_to: Annotated[
        Path | None,
        typer.Option("--materialise", help="Write every case out as a real git repo."),
    ] = None,
    list_cases: Annotated[bool, typer.Option("--list", help="List the corpus.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", help="Show per-case detail.")] = False,
) -> None:
    """Score a reviewer against the synthetic corpus."""
    from eval.case import discover_cases, materialise
    from eval.score import from_json_file, score_all

    if not CORPUS.is_dir():
        console.print("[red]No corpus.[/red] Run: python eval/build_corpus.py")
        raise typer.Exit(code=2)

    cases = discover_cases(CORPUS)

    if list_cases:
        _list(cases)
        raise typer.Exit(code=0)

    if materialise_to is not None:
        materialise_to.mkdir(parents=True, exist_ok=True)
        for child in sorted(CORPUS.iterdir()):
            if child.is_dir() and (child / "case.yaml").is_file():
                built = materialise(child, materialise_to)
                console.print(
                    f"  [cyan]{built.case.id}[/cyan] "
                    f"[dim]{built.base[:8]}..{built.head[:8]}  {built.repo}[/dim]"
                )
        console.print()
        console.print(
            f"[bold]{len(cases)} repos written.[/bold] Push these as merge requests to run "
            "another reviewer over the same cases."
        )
        raise typer.Exit(code=0)

    if from_json is None:
        console.print("[yellow]Nothing to score.[/yellow]")
        console.print("  --list                list the corpus")
        console.print("  --materialise DIR     write the cases out as git repos")
        console.print("  --from-json FILE      score a reviewer's output")
        console.print()
        console.print("[dim]Scoring Habodek itself needs the agent loop (M1).[/dim]")
        raise typer.Exit(code=0)

    report = score_all(cases, from_json_file(from_json))
    _summary(report)
    if verbose:
        _detail(report)


def _list(cases) -> None:
    table = Table(title="Synthetic corpus", title_justify="left", header_style="bold")
    table.add_column("Case", style="cyan")
    table.add_column("Planted", justify="right")
    table.add_column("Categories")
    table.add_column("Title")
    for case in cases:
        categories = ", ".join(sorted({e.category for e in case.expect})) or "[dim]clean[/dim]"
        table.add_row(
            case.id, str(len(case.expect)) if case.expect else "-", categories, case.title
        )
    console.print(table)
    console.print()
    console.print(
        f"[bold]{len(cases)}[/bold] cases, "
        f"[bold]{sum(len(c.expect) for c in cases)}[/bold] planted flaws, "
        f"[bold]{sum(1 for c in cases if c.is_clean)}[/bold] clean."
    )


def _summary(report) -> None:
    table = Table(title="Results", title_justify="left", header_style="bold")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    colour = "green" if report.clean_case_noise == 0 else "red"
    table.add_row(
        "Noise on clean cases",
        f"[{colour}]{report.clean_case_noise}[/{colour}] "
        f"[dim](over {report.clean_cases} clean cases)[/dim]",
    )
    table.add_row("Precision", f"{report.precision:.0%}")
    table.add_row("Detection", f"{report.detection:.0%} ({report.found}/{report.planted})")
    table.add_row("F1", f"{report.f1:.2f}")
    table.add_row("Total reported", str(report.reported))
    if report.found:
        table.add_row(
            "Anchored on the flaw",
            f"{report.exact_anchors}/{report.found} "
            f"[dim](mean {report.mean_anchor_distance:.1f} lines off)[/dim]",
        )
    console.print()
    console.print(table)

    if report.clean_case_noise:
        console.print()
        console.print(
            "[red]This reviewer comments on changes with nothing wrong.[/red] "
            "That is the failure mode that gets a bot muted."
        )


def _detail(report) -> None:
    console.print()
    for case_score in report.scores:
        marker = "[green]OK[/green]" if case_score.perfect else "[yellow]--[/yellow]"
        label = " [dim](clean)[/dim]" if case_score.is_clean else ""
        console.print(f"{marker} [cyan]{case_score.case_id}[/cyan]{label}")
        for expectation_id, finding in case_score.hits:
            off = case_score.distances.get(expectation_id, 0)
            drift = f" [yellow](anchored {off} off)[/yellow]" if off else ""
            console.print(
                f"     [green]found[/green]  {expectation_id} at {finding.location}{drift}"
            )
        for missed in case_score.missed:
            console.print(f"     [red]missed[/red] {missed.id} at {missed.file}:{missed.lines}")
        for extra in case_score.unmatched:
            console.print(f"     [yellow]noise[/yellow]  {extra.location} {extra.title[:60]}")
