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
                                                                           LiteLLM
```

## What triggers what

| GitLab event | Task | Priority |
|---|---|---|
| The trigger label (`REVIEW_TRIGGER_LABEL`, default `ai-review`) added, or an MR opened with it | review the MR at its head | review |
| A human note in a thread the bot opened | answer it (debounced `HABODEK_REPLY_DEBOUNCE_SEC`, default 60 s) | reply |
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

Per-model behaviour, roles and prompts are configured in the brain
(`habodek/brain/llm/models.yaml`), not here.

## Commands

```bash
go run ./cmd/server                              # the service
go run ./cmd/cli review --project 42 --mr 7      # dry run: prints the comments, posts nothing
go run ./cmd/cli clone-all                       # pre-warm the mirrors
```

## Where things are

- `internal/orchestrator` — queue and finding store (`store.go`), webhook triggers,
  worker, poster (GitLab position rules, diff_refs check, file-level fallback,
  idempotent hidden markers).
- `internal/core/brain` — the JSON contract with the brain, the subprocess runner,
  worktrees. `testdata/` holds golden files that the Python tests also check.
- `internal/pkg/llmproxy` — `POST /internal/llm/v1/chat/completions`.
- `internal/pkg/gitlab`, `internal/pkg/git` — GitLab REST and git transport.

## Tests

```bash
GOFLAGS=-mod=vendor go test -race ./...
HABODEK_TEST_POSTGRES='postgres://…?sslmode=disable' go test ./internal/orchestrator/   # + Postgres
HABODEK_E2E=1 go test -run TestEndToEnd ./internal/orchestrator/   # with the real brain
```

The end-to-end test runs label → in-progress note → a thread with its evidence chain
→ a reply → a verdict, against a fake GitLab, the real brain and a scripted gateway.
