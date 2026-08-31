"""Habodek's Python side: the evaluation harness.

The reviewer itself is the Go agent in `agent/`. What lives here is the instrument
that tells us whether a change to it was an improvement — a corpus of code changes
with deliberately planted flaws, and a scorer that grades any reviewer's output.

It is deliberately reviewer-agnostic. It was built to compare candidates before we
picked one, and it stays useful as the regression check on prompt and model
changes: "it felt better" is not evidence.
"""

from __future__ import annotations

import typer

from habodek.cli_eval import register as register_eval

app = typer.Typer(
    name="habodek",
    help="Evaluation harness for the Habodek review agent.",
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


if __name__ == "__main__":
    app()
