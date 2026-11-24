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
import os
import sys
import time
from pathlib import Path

from habodek.brain import __version__
from habodek.brain.contract import json_schemas

_LATER = {"map": "M10", "replay": "M11", "probe": "M11"}


def _home() -> Path:
    return Path(os.environ.get("HABODEK_HOME", Path.home() / ".habodek"))


def _client(trace, limiter=None):
    from habodek.brain.config import Config
    from habodek.brain.llm.client import LLMClient
    from habodek.brain.llm.limiter import RateLimiter
    from habodek.brain.llm.profiles import load_profiles

    config = Config.from_env()
    limiter = limiter or RateLimiter(config.rpm, burst=config.rpm_burst)
    return LLMClient(config.llm_base_url or None, config.llm_token,
                     profiles=load_profiles(config.models_yaml), limiter=limiter, trace=trace,
                     timeout=config.request_timeout_s)


def _review_config():
    from habodek.brain.config import Config
    from habodek.brain.workflow.pipeline import ReviewConfig

    config = Config.from_env()
    return ReviewConfig(evidence_tokens=config.evidence_pack_tokens,
                        internal_packages=config.internal_packages)


def _run_job(job, ws, trace_dir: Path, limiter=None):
    from habodek.brain.thread.agent import run_thread
    from habodek.brain.trace.writer import TraceWriter
    from habodek.brain.workflow.pipeline import run_review

    trace = TraceWriter(trace_dir)
    with _client(trace, limiter) as client:
        if job.kind == "review":
            return run_review(job, ws, client, trace, _review_config())
        return run_thread(job, ws, client, trace)


def _cmd_job(args: argparse.Namespace) -> int:
    """`review` and `thread`: one job.json in, one result.json out."""
    from habodek.brain.jobs import load_job, workspace_from_job

    job = load_job(args.job)
    if job.kind != args.command:
        print(f"{args.job} is a {job.kind} job; use `habodek-brain {job.kind}`", file=sys.stderr)
        return 2
    result = _run_job(job, workspace_from_job(job), Path(args.trace or "trace"))
    Path(args.out or "result.json").write_text(result.model_dump_json(indent=2))
    if result.thread:
        print(f"{result.status}: {result.thread.verdict}, {result.requests} requests")
    else:
        print(f"{result.status}: {len(result.comments)} comments, {result.requests} requests")
    return 0 if result.status in ("ok", "skipped") else 1


def _build_dir(args: argparse.Namespace) -> Path:
    from eval.scenarios.build import build

    return build(Path(args.build) if args.build else _home() / "scenarios")


def _cmd_local_job(args: argparse.Namespace) -> int:
    from eval.scenarios.prepare import prepare

    built = _build_dir(args)
    work = Path(args.work or (_home() / "work"))
    job, ws = prepare(built, args.scenario, work, f"local-{int(time.time())}")
    trace_dir = Path(args.trace or (work / f"trace-s{args.scenario}"))
    result = _run_job(job, ws, trace_dir)
    out = Path(args.out or (trace_dir / "result.json"))
    out.write_text(result.model_dump_json(indent=2))
    what = result.thread.verdict if result.thread else f"{len(result.comments)} comments"
    print(f"S{args.scenario} {result.status}: {what}, {result.requests} requests -> {out}")
    return 0 if result.status in ("ok", "skipped") else 1


def _cmd_scorecard(args: argparse.Namespace) -> int:
    from eval.scenarios.specs import select
    from habodek.brain.config import Config
    from habodek.brain.llm.limiter import RateLimiter
    from habodek.brain.llm.profiles import load_profiles
    from habodek.brain.trace.scorecard import run_scorecard

    config = Config.from_env()
    limiter = RateLimiter(config.rpm, burst=config.rpm_burst)  # shared by every run
    roles = load_profiles(config.models_yaml).roles
    models_line = (f"inv={roles.get('investigator')} fin/verA={roles.get('finder')}"
                   if roles.get("finder") == roles.get("verifier_a")
                   else f"inv={roles.get('investigator')} fin={roles.get('finder')} "
                        f"verA={roles.get('verifier_a')}")
    out = Path(args.out or (_home() / "scorecards" / time.strftime("%Y-%m-%dT%H-%M")))
    lines = run_scorecard(select(args.scenarios), args.runs, out, _build_dir(args),
                          lambda job, ws, run_dir: _run_job(job, ws, run_dir / "trace",
                                                            limiter), models_line)
    print("\n".join(lines))
    return 0


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

    for name, text in (("review", "review a merge request (job.json -> result.json)"),
                       ("thread", "answer a reply or re-check a finding after a push")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--job", required=True)
        p.add_argument("--out")
        p.add_argument("--trace")
        p.set_defaults(func=_cmd_job)
    p = sub.add_parser("map")
    p.add_argument("--job", required=True)
    p.add_argument("--out")
    p.add_argument("--trace")
    p = sub.add_parser("local-job", help="run one scenario against the configured gateway")
    p.add_argument("--scenario", required=True)
    p.add_argument("--build", help="scenario build dir (default $HABODEK_HOME/scenarios)")
    p.add_argument("--work")
    p.add_argument("--out")
    p.add_argument("--trace")
    p.set_defaults(func=_cmd_local_job)
    p = sub.add_parser("scorecard", help="run scenarios N times and print <= 20 lines")
    p.add_argument("--scenarios", default="1-4")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--build")
    p.add_argument("--out")
    p.set_defaults(func=_cmd_scorecard)
    for name in ("replay", "probe"):
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
