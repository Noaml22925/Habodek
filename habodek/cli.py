"""`habodek`: the v1 evaluation harness, and the trace viewer.

The reviewer is the brain (`habodek-brain`, `habodek/brain/`) run by the Go control
plane in `agent/`. This CLI keeps the v1 corpus harness (`habodek eval`) and offers
`habodek trace show RUN`, the same viewer as `habodek-brain trace show`.
"""

from __future__ import annotations

import typer

from habodek.cli_eval import register as register_eval

app = typer.Typer(
    name="habodek",
    help="Habodek: the v1 evaluation harness and the brain's trace viewer.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Keeps subcommand dispatch explicit.

    Without a callback, Typer collapses a single-command app and `habodek eval`
    would be rejected as an unexpected argument.
    """


register_eval(app)

trace_app = typer.Typer(help="Look at a brain run's trace.", no_args_is_help=True)
app.add_typer(trace_app, name="trace")


@trace_app.command("show")
def trace_show(run: str = typer.Argument(..., help="the run's trace directory"),
               out: str = typer.Option(None, help="output file (default RUN/trace.html)")) -> None:
    """Render a run as one static HTML file: hypothesis tree, tool calls, verdicts."""
    from habodek.brain.trace.view import show

    typer.echo(str(show(run, out)))


if __name__ == "__main__":
    app()
