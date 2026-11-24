"""Serve the scripted model as an OpenAI-compatible gateway, for the Go e2e test.

    python tests/e2e_gateway.py --built DIR

Builds the scenarios into DIR if needed, prints the gateway URL on its first line,
and serves until killed. Every request is rendered through the real chat templates.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import chat_template as ct  # noqa: E402
from scripted_model import ScriptedModel  # noqa: E402

from eval.scenarios.build import build  # noqa: E402
from habodek.brain.llm.fake_gateway import FakeGateway  # noqa: E402
from habodek.brain.llm.profiles import load_profiles  # noqa: E402

PROFILES = load_profiles(env={})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--built", required=True)
    args = parser.parse_args()
    built = build(Path(args.built))
    gateway = FakeGateway(ScriptedModel(built),
                          validator=lambda model, body: ct.render(PROFILES.get(model).name, body))
    with gateway.serve() as url:
        print(url, flush=True)
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
