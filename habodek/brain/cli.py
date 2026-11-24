"""`habodek-brain`: one subprocess per job.

    habodek-brain review  --job job.json --out result.json --trace DIR
    habodek-brain thread  --job job.json --out result.json --trace DIR
    habodek-brain schema  [--out DIR]          JSON Schema of job.json / result.json
    habodek-brain fake-gateway --script S.json  a scripted gateway for offline runs
    habodek-brain map | replay | probe | scorecard | local-job   (later milestones)

Exit codes: 0 ok (including a skipped job), 1 the job failed (result.json says why),
2 usage error or not implemented yet.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from habodek.brain import __version__
from habodek.brain.contract import json_schemas

_LATER = {
    "review": "M6", "thread": "M7", "map": "M10", "replay": "M11", "probe": "M11",
    "scorecard": "M5", "local-job": "M6",
}


def _cmd_schema(args: argparse.Namespace) -> int:
    schemas = json_schemas()
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for name, schema in schemas.items():
            (out / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
        print(f"wrote {', '.join(sorted(schemas))} schemas to {out}")
    else:
        json.dump(schemas, sys.stdout, indent=2)
        print()
    return 0


def _cmd_fake_gateway(args: argparse.Namespace) -> int:
    import time

    from habodek.brain.llm.fake_gateway import FakeGateway, Reply

    raw = json.loads(Path(args.script).read_text()) if args.script else []

    def to_reply(item: dict) -> Reply:
        item = dict(item)
        item["tool_calls"] = [tuple(pair) for pair in item.get("tool_calls", [])]
        return Reply(**item)

    script = ({k: [to_reply(i) for i in v] for k, v in raw.items()} if isinstance(raw, dict)
              else [to_reply(i) for i in raw])
    gateway = FakeGateway(script)
    with gateway.serve(port=args.port) as url:
        print(url, flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="habodek-brain", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"habodek-brain {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("review", "thread", "map", "local-job"):
        p = sub.add_parser(name)
        p.add_argument("--job", required=name != "local-job")
        p.add_argument("--out")
        p.add_argument("--trace")
    for name in ("replay", "probe", "scorecard"):
        p = sub.add_parser(name)
        p.add_argument("rest", nargs=argparse.REMAINDER)

    p = sub.add_parser("schema", help="export the job/result JSON Schemas")
    p.add_argument("--out")
    p.set_defaults(func=_cmd_schema)

    p = sub.add_parser("fake-gateway", help="serve a scripted OpenAI-compatible gateway")
    p.add_argument("--script")
    p.add_argument("--port", type=int, default=0)
    p.set_defaults(func=_cmd_fake_gateway)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        print(f"habodek-brain {args.command}: not implemented yet (milestone "
              f"{_LATER.get(args.command, '?')})", file=sys.stderr)
        return 2
    return int(func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
