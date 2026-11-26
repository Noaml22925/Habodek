# Habodek

Automatic code review for our GitLab merge requests, on the company LiteLLM gateway,
running unattended inside the VPN.

The bar is a reviewer whose reasoning matches opening Claude Code on the repository:
it reads the change and the code around it, follows what the change touches across
files, packages and libraries, forms hypotheses, settles each one with evidence (and,
where it can, by running the tests), has two independent verifiers try to refute
what survives, and comments on the exact changed line with a `file:line` evidence
chain. When a developer replies or pushes a fix, it goes back to the code and states a
verdict. It never resolves threads; the developer decides. Silence on a clean MR is
the headline property.

**State (2026-11-26):** reviewer v2 is built (milestones M0-M12) on branch
`habodek-brain-rework`; next is deployment on the VPN and the acceptance test —
start with [`HANDOFF.md`](HANDOFF.md), then [`DEPLOY.md`](DEPLOY.md).
[`ROADMAP.md`](ROADMAP.md) is the handoff document and decision record.

## Layout

```
agent/            The control plane (Go): GitLab webhooks, durable queue, worktrees,
                  the LLM proxy (per-model rate limits, raw traffic), the sandbox
                  service, memory bookkeeping, posting. It decides nothing about code.
habodek/brain/    The brain (Python), one subprocess per job (`habodek-brain`):
                  code intelligence, the agent loop, the review workflow, the thread
                  agent, the cartographer, memory, traces, replay.
eval/scenarios/   Nine generated multi-repository scenarios with expected outcomes,
                  and reference reviews written with Claude Code.
eval/corpus/      The v1 evaluation corpus (`habodek eval`), kept for history.
probe/            The one-file gateway probe the owner runs on the VPN.
docker/           Dockerfiles (brain, control, runner), compose, the VPN build script,
                  the egress test.
docs/             The v2 design, its implementation plan, and the survey behind it.
```

## How a review runs

```
GitLab --webhook--> control plane --job.json--> habodek-brain --result.json--> control plane --> GitLab
                         |  LLM proxy <------------ requests ----|   |
                         |  sandbox   <------------ run(cmd) ----+   |
                         |  memory    <--- memory-apply (cards, lessons)
```

1. A label (default `ai-review`) queues a review. The control plane fetches the MR,
   prepares worktrees for head and merge base, the internal dependencies at the pinned
   versions, and — for a library — the repositories that use it.
2. The brain builds an evidence pack, runs six finder lenses, investigates each
   hypothesis, checks every quote and anchor mechanically, and has verifier A and then
   verifier B try to refute each finding. Only what both uphold is posted.
3. Replies and pushes start the thread agent: fixed, fix incomplete, stands,
   withdrawn (with a lesson), or a question.

Details: [`agent/README.md`](agent/README.md) (configuration, triggers, endpoints) and
[`docs/reviewer-v2-handoff.md`](docs/reviewer-v2-handoff.md) (the plan and decisions).

## Development

No gateway is reachable from development; everything is tested offline against a
scripted gateway that renders every request through the models' real chat templates.

```bash
pip install -e ".[dev]"
pytest -q && ruff check .
cd agent && go build ./... && go vet ./... && go test -race ./...
HABODEK_E2E=1 go test -run TestEndToEnd ./internal/orchestrator/   # with the real brain
```

## Looking at a run

Every job leaves a trace directory (`HABODEK_TRACE_DIR/<job id>/`): `run.json` (the
inputs), `events.jsonl` (every request, response and tool call), `findings.json`
(every hypothesis and where it ended), `metrics.json`, `result.json`.

```bash
habodek-brain trace show RUN                       # one static HTML file: the hypothesis tree,
                                                   # tool calls, verifier arguments
habodek-brain replay RUN                           # re-run from the recorded traffic
habodek-brain replay RUN --from-stage verify       # ... with verification live
habodek-brain replay RUN --model verifier_a=MODEL  # ... with another model for one role
habodek-brain probe RUN --hypothesis "..."         # would it have found this?
cd agent && go run ./cmd/cli metrics --project ID  # acceptance per category, raised bars
```

On the VPN, the scenario scorecard prints at most 20 lines:
`habodek-brain scorecard --scenarios 1-9 --runs 3`.
