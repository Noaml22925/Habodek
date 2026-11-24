"""Role prompts: Markdown/Jinja2 templates in `roles/`, rendered with strict variables.

Prompts are written generically. They never name a scenario or corpus case (a prompt
that names its test set is fitted to it).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import jinja2

ROLES_DIR = Path(__file__).with_name("roles")

_env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROLES_DIR)),
                          undefined=jinja2.StrictUndefined, keep_trailing_newline=False,
                          trim_blocks=True, lstrip_blocks=True, autoescape=False)


def render(name: str, **variables: object) -> str:
    return _env.get_template(f"{name}.md").render(**variables).strip() + "\n"


@dataclass(frozen=True)
class Lens:
    name: str
    title: str
    focus: tuple[str, ...]


# Six lenses plus an optional seventh (handoff §7 M6). Each list stays short: a long
# checklist inside one prompt gets skipped; a new concern becomes a new lens.
LENSES: tuple[Lens, ...] = (
    Lens("intent", "intent and contracts, including removed behaviour", (
        "Does the change do what the title and description say, completely?",
        "Which contracts change: return values, exceptions, None, defaults, units, order?",
        "For every removed (`-`) line: what did it enforce, and where is that now done?",
        "Do the tests that changed still test what they used to, or were they weakened?",
    )),
    Lens("consumers", "consumers, in both directions, including dependencies", (
        "Find every caller or reader of each changed symbol, in this repository and in "
        "the other repositories and dependency roots you can read.",
        "Check each one against the new contract, line by line.",
        "For a changed dependency version, read what changed in the library and every "
        "place this repository uses that API.",
        "For new code, check what it calls against the callee's actual contract.",
    )),
    Lens("correctness", "correctness in the enclosing function", (
        "Read each changed function whole: off-by-one, wrong condition, wrong variable, "
        "inverted check, missing case, wrong default.",
        "Language pitfalls: mutable defaults, late binding, truthiness of 0/''/None, "
        "integer vs float, shadowing, iteration while mutating.",
        "Sibling consistency: is the same thing done differently elsewhere in the file?",
    )),
    Lens("failure", "failure paths: errors, resources, concurrency, wrappers and caches", (
        "What happens on each error path the change touches: caught, propagated, logged?",
        "Resources opened and not closed; transactions or locks not released.",
        "Shared state touched from several threads or requests.",
        "Caches, retries and wrappers around the changed code: do they still behave?",
    )),
    Lens("security", "security and data", (
        "Who can reach the changed code (routes, jobs, inputs) and with what checks: "
        "authentication, authorisation, ownership of the object addressed.",
        "Untrusted input reaching queries, files, commands, templates or redirects.",
        "Secrets, personal data or internal details in responses or logs.",
    )),
    Lens("history", "history and rules", (
        "`git_log` / `git_blame` on the touched lines: was this code fixed before, and "
        "does the change undo that fix?",
        "Project rules in `.habodek.md` or the README that the change contradicts.",
        "Documented behaviour (docstrings, README, changelog) the change breaks.",
    )),
    Lens("counterframe", "counter-frame: attacker, on-call engineer, maintainer", (
        "As an attacker: what does this change let me do that I could not before?",
        "As the on-call engineer at 3 a.m.: how does this fail in production, and is it "
        "visible?",
        "As the maintainer in six months: what does the description not mention?",
    )),
)
