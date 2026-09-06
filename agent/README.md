# Habodek agent

Fork of [gitlab-review-agent](https://github.com/antlss/gitlab-review-agent) (MIT).
Provenance and our accumulated changes: [`NOTICE.md`](NOTICE.md).
Upstream's own feature documentation is preserved at
[`README.upstream.md`](README.upstream.md) and is still the best reference for
options this file does not cover.

## Pointing it at our LiteLLM gateway

The OpenAI client here is hand-rolled `net/http` — a plain
`POST {base}/chat/completions` with `Authorization: Bearer`, a standard `tools`
array and standard `tool_calls` parsing. Any OpenAI-compatible endpoint works, so
our gateway needs **no code change**, only configuration:

```bash
LLM_DEFAULT_PROVIDER=openai
LLM_DEFAULT_MODEL=qwen3.6
OPENAI_BASE_URL=https://litellm.internal/v1
OPENAI_API_KEY=sk-...
```

Two things to know:

* **Context window.** `resolveContextWindow` (`internal/pkg/llm/client.go`) looks
  the model up in `defaultContextWindows` (`internal/config/config.go`) and falls
  back to **128k** for anything unknown. That is correct for qwen3.6. For a model
  with a larger window, add a line to that map or it silently under-uses context.
* **`max_completion_tokens`.** `internal/pkg/llm/openai.go` sends the newer field
  name. LiteLLM normally translates it; if a model rejects it, change that one
  word to `max_tokens`.

`OPENAI_API_KEYS` (plural) accepts a comma-separated list and load-balances across
them. Per-repository model overrides are supported through repository settings, so
different teams can run different models.

## GitLab

```bash
GITLAB_BASE_URL=https://gitlab.internal
GITLAB_TOKEN=...              # group or project access token, api scope
GITLAB_WEBHOOK_SECRET=...
GITLAB_BOT_USER_ID=...        # the bot's own user id — the loop guard
```

`GITLAB_BOT_USER_ID` matters: it is how the agent ignores its own notes. Get it
wrong and it will reply to itself.

Webhook: Merge request events + Comment events → `http://<host>:8080/webhook`.

## Triggering

Reviews are **label-triggered**, not automatic on push — add `ai-review`
(`REVIEW_TRIGGER_LABEL`) to an MR. This is the cost control: nothing runs unless
somebody asks. The CLI bypasses the label check and always reviews.

Replies are handled automatically: when a human comments in one of the agent's own
threads, the replier agent wakes up with the thread history and the code at HEAD,
and answers with a verdict on its own finding — it stands, it is withdrawn, or the
code now fixes it. After a push, every open thread gets the same treatment against
the new code (`REVIEW_THREAD_VERDICTS`).

The agent never resolves a thread. It states a conclusion; you decide.

## Cost and size controls

| Variable | Default | Effect |
|---|---|---|
| `REVIEW_MAX_FILES_BEFORE_SAMPLE` | 150 | Above this, sample rather than review everything |
| `REVIEW_LARGE_PR_ACTION` | `sample` | `sample` or `block` |
| `REVIEW_CHUNK_THRESHOLD` | 21 | File count that switches unified → chunked review |
| `REVIEW_TRIAGE_THRESHOLD` | 81 | File count that switches chunked → triage |
| `REVIEW_MAX_PARALLEL_CHUNKS` | 5 | Concurrency, and therefore burst token spend |
| `REVIEW_MAX_FINDINGS` | 10 | Hard cap on comments posted per review |
| `REVIEW_THREAD_VERDICTS` | `true` | Re-check open threads after a push; one LLM call each |
| `REVIEW_MAX_VERDICT_THREADS` | 20 | Ceiling on re-checks per review |
| `LLM_CONTEXT_COMPRESSION_THRESHOLD` | 0.55 | Fraction of context before compression |

`REVIEW_MAX_FINDINGS` is the noise dial. Start it low. A reviewer that posts thirty
threads on one MR gets muted within a fortnight, and a muted reviewer reviews
nothing.

The cap ranks before it cuts — severity, then confidence, then file risk — so
criticals survive it. What it does not do is defer: a finding cut by the budget
is gone, because the next review diffs from this head and never revisits those
lines. `go run ./cmd/cli findings --project-id <id> --mr-id <iid>` shows what was
dropped and by which filter.

## Where things are

```
cmd/server            webhook server
cmd/cli               interactive dry-run review, and clone-all
internal/config       every env var, and defaultContextWindows
internal/pkg/llm      openai.go / anthropic.go / google.go, balancer, retry
internal/pkg/tools    read_file, search_code, read_multi_file
internal/pkg/gitlab   discussions, notes, ResolveDiscussion
internal/core/prompt  THE PROMPTS — plain string constants, edit as text
internal/core/review  pipeline, planner, risk scoring, chunking, output parsing
internal/core/reply   intent classification and the reply pipeline
internal/core/agents  reviewer/ and replier/
internal/handler      webhook, worker pool, cron
internal/pkg/store    file / sqlite / postgres backends
```

**To change what the reviewer looks for, edit `internal/core/prompt/`.** Those are
Go string constants containing markdown — editing them needs no Go.

## Maintaining this

We are the only maintainer; upstream is dormant. Practical notes:

* Run `go test -race ./...`, not just `go test`. The worker pool, queue and cron
  are genuinely concurrent, and races do not reliably fail a normal test run. This
  is the one part of the codebase where Python intuition actively misleads: Go has
  real parallelism and no GIL.
* The compiler is the safety net. Wrong Go fails `go build` in seconds.
* **SQLite needs CGO, and this machine has no C compiler** — so `CGO_ENABLED=0`,
  `go-sqlite3` compiles to a stub, and its store tests fail. Confirmed, not
  theoretical. Use `STORE_DRIVER=file` locally; the Dockerfile sets
  `CGO_ENABLED=1` and builds it properly.
* **`go test -race` will not run here either** — the race detector also requires
  cgo. Run it in Docker or WSL, or install a toolchain
  (`winget install BrechtSanders.WinLibs.POSIX.UCRT`) and set `CGO_ENABLED=1`.
* Score prompt changes with the harness in [`../eval`](../eval) before shipping
  them. "It felt better" is not evidence.
