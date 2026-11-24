"""`habodek-brain`: one subprocess per job.

    habodek-brain review  --job job.json --out result.json --trace DIR
    habodek-brain thread  --job job.json --out result.json --trace DIR
    habodek-brain schema  [--out DIR]          JSON Schema of job.json / result.json
    habodek-brain fake-gateway --script S.json  a scripted gateway for offline runs
    habodek-brain map     --job job.json --out result.json --trace DIR   the cartographer
    habodek-brain memory-apply --repo DIR --result result.json     commit memory updates
    habodek-brain trace show RUN [--out FILE]                      static HTML of a run
    habodek-brain replay RUN [--model ROLE=M] [--from-stage S]     re-run from its cassette
    habodek-brain probe RUN --hypothesis "..." [--verify]          one investigator, one idea

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

_LATER: dict[str, str] = {}


def _home() -> Path:
    return Path(os.environ.get("HABODEK_HOME", Path.home() / ".habodek"))


def _client(trace, limiter=None, transport=None, base_url=None):
    from habodek.brain.config import Config
    from habodek.brain.llm.client import LLMClient
    from habodek.brain.llm.limiter import LimiterPool
    from habodek.brain.llm.profiles import load_profiles

    config = Config.from_env()
    limiter = limiter or LimiterPool(config.rpm, burst=config.rpm_burst)
    return LLMClient(base_url or config.llm_base_url or None, config.llm_token,
                     profiles=load_profiles(config.models_yaml), limiter=limiter, trace=trace,
                     timeout=config.request_timeout_s, transport=transport)


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


def _run_job(job, ws, trace_dir: Path, limiter=None, sandbox=None, transport=None,
             base_url=None):
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
    from habodek.brain.trace.runinfo import write_run_json

    memory = open_view(job)
    trace.event("memory", path=job.memory_path, commit=memory.commit if memory else "")
    with _client(trace, limiter, transport, base_url) as client:
        write_run_json(trace, job, ws, dict(client.profiles.roles),
                       memory.commit if memory else "", sandbox.name if sandbox else "none")
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


def _cmd_trace(args: argparse.Namespace) -> int:
    from habodek.brain.trace.view import show

    print(show(args.run, args.out))
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    """Re-run a recorded run: from its cassette, or live from a stage / with a model."""
    from habodek.brain.trace import replay as rp

    run_dir = Path(args.run)
    info = rp.read_run(run_dir)
    out = Path(args.out or (run_dir.parent / f"{run_dir.name}-replay-{int(time.time())}"))
    job, ws, notes = rp.rebuild_workspace(info, out / "work")
    for spec in args.model or []:
        role, _, model = spec.partition("=")
        job.models.roles[role.strip()] = model.strip()
    live = rp.live_url() if (args.model or args.from_stage or args.live) else ""
    if (args.model or args.from_stage) and not live:
        print("replay: --model / --from-stage need a live gateway (HABODEK_LLM_BASE_URL)",
              file=sys.stderr)
        return 2
    transport = rp.CassetteTransport(rp.load_cassette(run_dir), live, args.from_stage)
    sandbox = rp.replay_sandbox(info, run_dir, ws, out / "sandbox-cache")
    limiter = None
    if not live:  # nothing leaves the process: the gateway's limits do not apply
        from habodek.brain.llm.limiter import LimiterPool

        limiter = LimiterPool(1_000_000, burst=1_000)
    result = _run_job(job, ws, out / "trace", limiter=limiter, sandbox=sandbox,
                      transport=transport, base_url=live or rp.CASSETTE_URL)
    (out / "trace" / "result.json").write_text(result.model_dump_json(indent=2))
    original = {}
    if (run_dir / "result.json").is_file():
        original = json.loads((run_dir / "result.json").read_text())
    t = transport.tally
    lines = [f"replay of {info.get('run_id')} ({info.get('kind')}): {result.status}, "
             f"cassette {t.hits} hit(s), live {t.live}, missed {len(t.misses)}"]
    lines += [f"  root {n}" for n in notes[:4]]
    if t.misses:
        lines.append(f"  first miss: {t.misses[0]} (the harness asked something new)")
    if result.kind == "review":
        before = sorted(c["finding_id"] for c in original.get("comments") or [])
        after = sorted(c.finding_id for c in result.comments)
        same = "same comments" if before == after else f"comments {before} -> {after}"
        lines.append(f"  {same}; requests {original.get('requests', '?')} -> {result.requests}")
    elif result.thread:
        was = (original.get("thread") or {}).get("verdict", "?")
        lines.append(f"  verdict {was} -> {result.thread.verdict}")
    lines.append(f"details: {out / 'trace'}")
    print("\n".join(lines))
    (out / "replay.json").write_text(json.dumps({"hits": t.hits, "live": t.live,
                                                 "misses": t.misses, "roots": notes}, indent=2))
    return 0 if not t.misses or live else 1


def _cmd_probe(args: argparse.Namespace) -> int:
    """One investigator on a stated hypothesis, in a recorded run's workspace."""
    from habodek.brain.memory.context import open_view
    from habodek.brain.trace import replay as rp
    from habodek.brain.trace.writer import TraceWriter
    from habodek.brain.workflow.pipeline import Hypothesis, Review

    run_dir = Path(args.run)
    info = rp.read_run(run_dir)
    if info.get("kind") != "review":
        print("probe: only review runs can be probed", file=sys.stderr)
        return 2
    out = Path(args.out or (run_dir.parent / f"{run_dir.name}-probe-{int(time.time())}"))
    job, ws, notes = rp.rebuild_workspace(info, out / "work")
    path, _, code = (args.changed_line or "").partition("::")
    trace = TraceWriter(out / "trace")
    with _client(trace) as client:
        review = Review(job, ws, client, trace, _review_config(), memory=open_view(job))
        review.prepare()
        h = Hypothesis(id="P1", claim=args.hypothesis.strip(), why="stated by a person (probe)",
                       severity=args.severity, changed_path=path.strip(),
                       changed_code=code.strip(), lenses=["probe"])
        stage = review._stage("investigate")
        confirmed = review.investigate([h], stage)
        lines = [f"probe of {info.get('run_id')}: {h.claim[:90]}"]
        if not confirmed:
            dropped = next((f for f in review.findings if f.hypothesis == "P1"), None)
            lines.append(f"  investigation: {dropped.drop_reason[:150] if dropped else '?'}")
        else:
            checked = review.check(confirmed)
            if not checked:
                f = review.findings[-1]
                lines.append(f"  confirmed, then dropped at {f.dropped_at}: {f.drop_reason[:120]}")
            else:
                f = checked[0]
                lines.append(f"  confirmed: {f.title[:120]}")
                lines.append(f"  anchor {f.anchor.path}:{f.anchor.line}; chain:")
                lines += [f"    {(x.repo + ':') if x.repo else ''}{x.path}:{x.line} "
                          f"{x.quote[:70]}" for x in f.chain[:6]]
                if args.verify:
                    upheld = review.verify(checked)
                    verdicts = ", ".join(f"{v.verifier} {v.outcome}" for v in f.verifications)
                    lines.append(f"  verifiers: {verdicts} -> "
                                 f"{'would post' if upheld else 'would not post'}")
        lines.append("  " + rp.original_outcome(run_dir, h.claim))
        lines.append(f"  requests {review.budget.used}")
        lines.append(f"details: {out / 'trace'}")
    trace.write_json("findings.json", [f.model_dump() for f in review.findings])
    print("\n".join(lines[:20]))
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
    p = sub.add_parser("trace", help="render a run as one static HTML file")
    tsub = p.add_subparsers(dest="trace_command", required=True)
    t = tsub.add_parser("show", help="write trace.html for a run directory")
    t.add_argument("run")
    t.add_argument("--out")
    t.set_defaults(func=_cmd_trace)
    p = sub.add_parser("replay", help="re-run a recorded run from its cassette")
    p.add_argument("run")
    p.add_argument("--out")
    p.add_argument("--model", action="append", metavar="ROLE=MODEL",
                   help="change a role's model (that role's requests go live)")
    p.add_argument("--from-stage", choices=["understand", "hypothesise", "investigate",
                                            "verify", "judge"],
                   help="send this stage and later ones to the live gateway")
    p.add_argument("--live", action="store_true",
                   help="answer cassette misses from the live gateway instead of failing")
    p.set_defaults(func=_cmd_replay)
    p = sub.add_parser("probe", help="investigate a stated hypothesis in a run's workspace")
    p.add_argument("run")
    p.add_argument("--hypothesis", required=True)
    p.add_argument("--changed-line", help="PATH::CODE of the changed line it is about")
    p.add_argument("--severity", choices=["critical", "major", "minor"], default="major")
    p.add_argument("--verify", action="store_true", help="also run both verifiers")
    p.add_argument("--out")
    p.set_defaults(func=_cmd_probe)

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
