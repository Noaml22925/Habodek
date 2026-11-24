"""`habodek-brain`: one subprocess per job.

    habodek-brain review  --job job.json --out result.json --trace DIR
    habodek-brain thread  --job job.json --out result.json --trace DIR
    habodek-brain schema  [--out DIR]          JSON Schema of job.json / result.json
    habodek-brain fake-gateway --script S.json  a scripted gateway for offline runs
    habodek-brain map     --job job.json --out result.json --trace DIR   the cartographer
    habodek-brain memory-apply --repo DIR --result result.json     commit memory updates
    habodek-brain replay | probe                                   (M11)

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

_LATER = {"replay": "M11", "probe": "M11"}


def _home() -> Path:
    return Path(os.environ.get("HABODEK_HOME", Path.home() / ".habodek"))


def _client(trace, limiter=None):
    from habodek.brain.config import Config
    from habodek.brain.llm.client import LLMClient
    from habodek.brain.llm.limiter import LimiterPool
    from habodek.brain.llm.profiles import load_profiles

    config = Config.from_env()
    limiter = limiter or LimiterPool(config.rpm, burst=config.rpm_burst)
    return LLMClient(config.llm_base_url or None, config.llm_token,
                     profiles=load_profiles(config.models_yaml), limiter=limiter, trace=trace,
                     timeout=config.request_timeout_s)


def _review_config():
    from habodek.brain.config import Config
    from habodek.brain.workflow.pipeline import ReviewConfig

    config = Config.from_env()
    return ReviewConfig(evidence_tokens=config.evidence_pack_tokens,
                        internal_packages=config.internal_packages)


def _sandbox(ws, mode: str | None = None, find_links: tuple[str, ...] = ()):
    """The job's sandbox, or None (then agents get no `run` tool).

    Production: the control plane sets HABODEK_SANDBOX_URL. Standalone runs choose with
    `--sandbox` or HABODEK_SANDBOX (local | auto | none)."""
    from habodek.brain.config import Config
    from habodek.brain.sandbox import sandbox_from_env

    config = Config.from_env()
    env = dict(os.environ)
    if mode:
        env["HABODEK_SANDBOX"] = mode
    roots = {"head": ws.head}
    if ws.base is not None:
        roots["base"] = ws.base
    roots.update({k: v for k, v in ws.extra_roots.items() if k.startswith("repo:")})
    return sandbox_from_env(env, roots, config.sandbox_cache, config.pip_index_url,
                            tuple(find_links) or config.pip_find_links)


def _run_job(job, ws, trace_dir: Path, limiter=None, sandbox=None):
    from habodek.brain.sandbox.core import RunLog, recipe_for
    from habodek.brain.thread.agent import run_thread
    from habodek.brain.trace.writer import TraceWriter
    from habodek.brain.workflow.pipeline import run_review

    trace = TraceWriter(trace_dir)
    runs = None
    if sandbox is not None:
        roots = ("head", *(("base",) if ws.base is not None else ()),
                 *sorted(k for k in ws.extra_roots if k.startswith("repo:")))
        runs = RunLog(sandbox, trace, recipe=recipe_for(ws.head, ws.base), roots=roots)
        trace.event("sandbox", backend=sandbox.name, roots=list(roots), recipe=runs.recipe)
    from habodek.brain.config import Config
    from habodek.brain.memory.context import open_view

    memory = open_view(job)
    trace.event("memory", path=job.memory_path, commit=memory.commit if memory else "")
    with _client(trace, limiter) as client:
        if job.kind == "map":
            from habodek.brain.memory.cartographer import run_map

            return run_map(job, ws, client, trace, memory, Config.from_env().internal_packages)
        if job.kind == "review":
            return run_review(job, ws, client, trace, _review_config(), runs=runs,
                              memory=memory)
        return run_thread(job, ws, client, trace, runs=runs, memory=memory)


def _cmd_job(args: argparse.Namespace) -> int:
    """`review` and `thread`: one job.json in, one result.json out."""
    from habodek.brain.config import Config
    from habodek.brain.jobs import attach_dependencies, load_job, workspace_from_job

    job = load_job(args.job)
    if job.kind != args.command:
        print(f"{args.job} is a {job.kind} job; use `habodek-brain {job.kind}`", file=sys.stderr)
        return 2
    ws = workspace_from_job(job)
    config = Config.from_env()
    log = attach_dependencies(job, ws, config.internal_packages, config.deps_cache,
                              config.pip_index_url, config.pip_find_links)
    for line in log:
        print(f"deps: {line}", file=sys.stderr)
    sandbox = _sandbox(ws) if job.kind != "map" else None  # the cartographer only reads
    result = _run_job(job, ws, Path(args.trace or "trace"), sandbox=sandbox)
    Path(args.out or "result.json").write_text(result.model_dump_json(indent=2))
    if result.kind == "map":
        print(f"{result.status}: {result.summary}, {result.requests} requests")
    elif result.thread:
        print(f"{result.status}: {result.thread.verdict}, {result.requests} requests")
    else:
        print(f"{result.status}: {len(result.comments)} comments, {result.requests} requests")
    return 0 if result.status in ("ok", "skipped") else 1


def _cmd_memory_apply(args: argparse.Namespace) -> int:
    """Merge a result's memory updates into the memory repository and commit them.

    The control plane runs this after a map job, and after a thread job whose
    withdrawn verdict carries a lesson; it prints the new memory commit."""
    from habodek.brain.contract import Result
    from habodek.brain.memory.store import apply_updates

    result = Result.model_validate_json(Path(args.result).read_text())
    if not result.memory_updates:
        print("no memory updates")
        return 0
    kinds = sorted({u.kind for u in result.memory_updates})
    project = result.memory_updates[0].project
    message = args.message or f"{'+'.join(kinds)} for {project} (job {result.job_id}, " \
                              f"run {result.run_id})"
    print(apply_updates(Path(args.repo), result.memory_updates, message))
    return 0


def _build_dir(args: argparse.Namespace) -> Path:
    from eval.scenarios.build import build

    return build(Path(args.build) if args.build else _home() / "scenarios")


def _cmd_local_job(args: argparse.Namespace) -> int:
    from eval.scenarios.prepare import prepare

    built = _build_dir(args)
    work = Path(args.work or (_home() / "work"))
    job, ws = prepare(built, args.scenario, work, f"local-{int(time.time())}")
    trace_dir = Path(args.trace or (work / f"trace-s{args.scenario}"))
    sandbox = _sandbox(ws, args.sandbox, (str(built / "index"),))
    result = _run_job(job, ws, trace_dir, sandbox=sandbox)
    out = Path(args.out or (trace_dir / "result.json"))
    out.write_text(result.model_dump_json(indent=2))
    what = result.thread.verdict if result.thread else f"{len(result.comments)} comments"
    print(f"S{args.scenario} {result.status}: {what}, {result.requests} requests -> {out}")
    return 0 if result.status in ("ok", "skipped") else 1


def _cmd_scorecard(args: argparse.Namespace) -> int:
    from eval.scenarios.specs import select
    from habodek.brain.config import Config
    from habodek.brain.llm.limiter import LimiterPool
    from habodek.brain.llm.profiles import load_profiles
    from habodek.brain.trace.scorecard import run_scorecard

    config = Config.from_env()
    limiter = LimiterPool(config.rpm, burst=config.rpm_burst)  # per model, shared by every run
    roles = load_profiles(config.models_yaml).roles
    models_line = (f"inv={roles.get('investigator')} fin/verA={roles.get('finder')}"
                   if roles.get("finder") == roles.get("verifier_a")
                   else f"inv={roles.get('investigator')} fin={roles.get('finder')} "
                        f"verA={roles.get('verifier_a')}")
    out = Path(args.out or (_home() / "scorecards" / time.strftime("%Y-%m-%dT%H-%M")))
    built = _build_dir(args)

    def runner(job, ws, run_dir):
        sandbox = _sandbox(ws, args.sandbox, (str(built / "index"),))
        return _run_job(job, ws, run_dir / "trace", limiter, sandbox)

    from habodek.brain.sandbox import local_available

    wanted = args.sandbox or os.environ.get("HABODEK_SANDBOX", "")
    sandbox_note = ("local" if wanted == "local" or (wanted == "auto" and local_available())
                    else "none")
    lines = run_scorecard(select(args.scenarios), args.runs, out, built, runner,
                          f"{models_line} sandbox={sandbox_note}")
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
    p = sub.add_parser("map", help="the cartographer: write a project's repo card")
    p.add_argument("--job", required=True)
    p.add_argument("--out")
    p.add_argument("--trace")
    p.set_defaults(func=_cmd_job)
    p = sub.add_parser("memory-apply", help="commit a result's memory updates")
    p.add_argument("--repo", required=True)
    p.add_argument("--result", required=True)
    p.add_argument("--message")
    p.set_defaults(func=_cmd_memory_apply)
    p = sub.add_parser("local-job", help="run one scenario against the configured gateway")
    p.add_argument("--scenario", required=True)
    p.add_argument("--build", help="scenario build dir (default $HABODEK_HOME/scenarios)")
    p.add_argument("--work")
    p.add_argument("--out")
    p.add_argument("--trace")
    p.add_argument("--sandbox", choices=["auto", "local", "none"], default="auto",
                   help="where agents' `run` tool executes (auto: local if unshare works)")
    p.set_defaults(func=_cmd_local_job)
    p = sub.add_parser("scorecard", help="run scenarios N times and print <= 20 lines")
    p.add_argument("--scenarios", default="1-4")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--build")
    p.add_argument("--out")
    p.add_argument("--sandbox", choices=["auto", "local", "none"], default="auto",
                   help="where agents' `run` tool executes (auto: local if unshare works)")
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
