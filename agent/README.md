# Habodek control plane (Go)

The part of Habodek that talks to GitLab and runs the reviewer. It does not decide
anything about code: the Python brain (`habodek/brain`, see the repository root)
does, one subprocess per job. This service fetches the bytes, queues the work, keeps
the brain away from every secret, and posts what the brain decided.

```
GitLab --webhook--> triggers --> durable queue --> worker --> worktrees --> habodek-brain
   ^                                                  |                        |
   |                                                  |                LLM proxy (per-model
   +-------- threads, replies <---- poster <---- result.json               limits, raw trace)
                                                                              |
                                                     sandbox (run tool) <-----+-----> LiteLLM
                                                  docker run --network none
```

## What triggers what

| GitLab event | Task | Priority |
|---|---|---|
| The trigger label (`REVIEW_TRIGGER_LABEL`, default `ai-review`) added, or an MR opened with it | review the MR at its head | review |
| A human note in a thread the bot opened | answer it (debounced `HABODEK_REPLY_DEBOUNCE_SEC`, default 60 s) | reply |
| The first review of a project not yet in memory | map it: the cartographer writes its repo card | background |
| A push to a mapped project's default branch that adds/removes files or changes dependency files or `.habodek.md` | re-map it (debounced `HABODEK_MAP_DEBOUNCE_MIN`, default 10) | background |
| New commits on a labelled MR | re-check every open finding whose evidence chain the commits touch | recheck |

Replies go ahead of re-checks, which go ahead of reviews. At most one task per thread
runs at a time, and a newer event replaces a queued one (a reply and a re-check never
both answer). The bot never resolves threads; it states a verdict and the developer
decides.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GITLAB_BASE_URL`, `GITLAB_TOKEN` | required | GitLab API access (stays in this process) |
| `GITLAB_WEBHOOK_SECRET`, `GITLAB_BOT_USER_ID` | required for the server | webhook check; recognising the bot's own notes |
| `HABODEK_GATEWAY_URL` (or `OPENAI_BASE_URL`) | required | the LiteLLM gateway |
| `HABODEK_GATEWAY_KEY` (or `OPENAI_API_KEY`) | | the gateway key (never passed to the brain) |
| `HABODEK_GATEWAY_RPM` | 25 | requests per minute **per model** |
| `STORE_DRIVER` | `sqlite` | `postgres` in production (`DATABASE_URL`), `sqlite` (`SQLITE_PATH`) otherwise |
| `WORKER_POOL_SIZE` | 3 | brain jobs at once |
| `HABODEK_BRAIN_CMD` | `habodek-brain` | the brain executable |
| `HABODEK_WORK_DIR`, `HABODEK_TRACE_DIR` | `data/jobs`, `data/traces` | worktrees (disposable), traces (kept) |
| `HABODEK_INTERNAL_PACKAGES` | | internal Python packages whose source the brain reads (names or globs) |
| `HABODEK_PIP_INDEX_URL` | | Artifactory index for that source |
| `HABODEK_MAX_COMMENTS` | 6 | comments per review |
| `GIT_REPOS_DIR` | `resource/repos` | project mirrors |
| `HABODEK_SANDBOX` | `none` | where the brain's `run` tool executes code: `docker` (production), `local` (`unshare`, dev boxes), `none` (no run tool) |
| `HABODEK_SANDBOX_IMAGE` | `habodek-runner:latest` | runner image (`docker/runner.Dockerfile`) |
| `HABODEK_SANDBOX_PREP_NETWORK` | `bridge` | Docker network env-prep uses to reach Artifactory (`HABODEK_PIP_INDEX_URL`); runs always get `--network none` |
| `HABODEK_SANDBOX_CACHE`, `HABODEK_SANDBOX_WORK` | `data/sandbox`, `data/sandbox-runs` | cached venvs (by dependency files + recipe), throwaway copies |
| `HABODEK_SANDBOX_MEMORY_MB`, `HABODEK_SANDBOX_CPUS`, `HABODEK_SANDBOX_PARALLEL` | 4096, 2, 2 | limits per run; runs at once |
| `HABODEK_MEMORY_DIR` | `data/memory` | the `habodek-memory` git repository (`none` disables memory) |
| `HABODEK_MAX_CONSUMERS` | 5 | consumer repositories checked out for a library MR |
| `HABODEK_MAP_DEBOUNCE_MIN` | 10 | how long a default-branch push waits before re-mapping |
| `HABODEK_BARS` | on | `off` disables automatic bar-raising |
| `HABODEK_BAR_MIN_OUTCOMES`, `HABODEK_BAR_WINDOW` | 5, 50 | decided threads needed before a bar moves; recent posted findings per category considered |
| `HABODEK_BAR_MAJOR_BELOW`, `HABODEK_BAR_CRITICAL_BELOW` | 0.5, 0.25 | acceptance below which a category posts only major+ / critical |
| `HABODEK_SANDBOX_URL` | `http://127.0.0.1:$SERVER_PORT/internal/sandbox/v1` | how the brain reaches the sandbox endpoint |

Per-model behaviour, roles and prompts are configured in the brain
(`habodek/brain/llm/models.yaml`), not here.

## Commands

```bash
go run ./cmd/server                              # the service
go run ./cmd/cli review --project 42 --mr 7      # dry run: prints the comments, posts nothing
go run ./cmd/cli map --project 42                # map a project into memory now (writes HABODEK_MEMORY_DIR)
go run ./cmd/cli metrics --project 42            # acceptance per category and any raised bars
go run ./cmd/cli clone-all                       # pre-warm the mirrors
```

## Where things are

- `internal/orchestrator` — queue and finding store (`store.go`), webhook triggers,
  worker, poster (GitLab position rules, diff_refs check, file-level fallback,
  idempotent hidden markers).
- `internal/core/brain` — the JSON contract with the brain, the subprocess runner,
  worktrees. `testdata/` holds golden files that the Python tests also check.
- `internal/pkg/llmproxy` — `POST /internal/llm/v1/chat/completions`.
- `internal/pkg/sandbox` — `POST /internal/sandbox/v1/run`: the job's token runs a
  command in a throwaway copy of `head`, `base` or `repo:<name>` (never a path), after
  env-prep builds that revision's venv (`envprep.py`, embedded; cached by the
  dependency files and the recipe). Backends: `Docker` (sibling containers) and
  `Local` (user + network namespace). Every run goes to the job's `sandbox.jsonl`.

- `internal/orchestrator/acceptance.go` — acceptance per category, the bars it
  raises, `GET /internal/metrics`.
- `internal/pkg/memory` — the control plane's view of `habodek-memory`: the commit
  each job reads (`memory_commit`), whether a project has a card, and the
  reverse-dependency index from every project's `facts.json`. Writes go through
  `habodek-brain memory-apply`, one at a time.

### Memory

`habodek-memory` is a git repository of markdown, one directory per project
(`projects/<group>/<project>/`): `card.md` (the cartographer's; edit only its Team
notes section, which survives a re-map), `facts.json`, and `lessons/*.md`. Lessons come
only from a `withdrawn` verdict that survived the challenge; each cites its threads.
A lesson becomes `active` (put in every agent's context) at its second independent
citation; set `status: active` or `status: retired` by hand to decide sooner. Every
job records the memory commit it read, so a replay sees the same memory.

**Libraries:** when a project's `facts.json` says it publishes a package, its MRs are
reviewed with up to `HABODEK_MAX_CONSUMERS` consumer repositories checked out — the
mapped projects whose default branch pins that package. A consumer is known only once
it has been mapped, so map consumers first (`cli map --project <id>`) or let their own
first review do it.

`.habodek.md` (any directory) is the team's: read from the target branch, applying to
the paths under its directory, and given only to the agents that propose findings —
never to those that verify, refute or post. Lines asking not to flag something are
dropped. It can add checks; it cannot clear one.

### Observability

Each job's trace directory (`HABODEK_TRACE_DIR/<job id>/`) holds `run.json` (the job,
every root's commit, versions, prompt hashes, roles, memory commit), `events.jsonl`
(every LLM request and response, tool call and stage), `llm-proxy.jsonl` (the raw
traffic this service forwarded), `sandbox.jsonl`, `findings.json`, `metrics.json` and
`result.json`. Worktrees are deleted after a job; `habodek-brain replay` and `probe`
rebuild them from `run.json`. `habodek-brain trace show <dir>` writes `trace.html`.

### Acceptance and raised bars

`GET /internal/metrics?project=<id>` (and `cli metrics`) reports, per category (a
finding's primary lens), how its posted findings fared: accepted (fixed, fix
incomplete, or stands after a verified re-check), withdrawn, neutral (unclear,
need info) and open. Once a category has 5 decided threads, acceptance below 50%
makes its review jobs post only major findings and above, below 25% only criticals.
Every raise is logged (`bar raised`), held-back findings are recorded with the
acceptance that held them back, and the MR summary says so. The bar falls again by
itself as acceptance recovers.

### The per-repository recipe

A repository can say how it is tested in `.habodek.md`, read from the **target
branch** (so an MR cannot change how it is itself tested):

    ```habodek-sandbox
    test: python -m pytest -q
    install: -r requirements/ci.txt
    tools: pytest-asyncio==0.23.8
    ```

`test` is the command suggested to the agents; `install` and `tools` add to what
env-prep installs (requirement files, `[project].dependencies` and the
test/dev groups are found without a recipe). Index options in requirement files and
recipes are dropped: packages come from `HABODEK_PIP_INDEX_URL` only.
- `internal/pkg/gitlab`, `internal/pkg/git` — GitLab REST and git transport.

## Tests

```bash
GOFLAGS=-mod=vendor go test -race ./...
HABODEK_TEST_POSTGRES='postgres://…?sslmode=disable' go test ./internal/orchestrator/   # + Postgres
HABODEK_E2E=1 go test -run TestEndToEnd ./internal/orchestrator/   # with the real brain
```

The end-to-end test runs label → in-progress note → a thread with its evidence chain
→ a reply → a verdict, against a fake GitLab, the real brain and a scripted gateway.
Where unprivileged user namespaces work it also routes the brain's test runs through
the sandbox endpoint, and checks the comment cites the run that fails at head.
