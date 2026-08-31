# Habodek Roadmap

Automatic code review for our GitLab merge requests, on the company LiteLLM
gateway. Reads the change *and its surrounding context*, opens threads on exact
diff lines, answers reviewer replies, resolves its own threads when addressed.

> **This file is the handoff document.** Context is reset between sessions and this
> is the only thing the next session gets. Keep it standing on its own: update it as
> part of finishing a milestone, and record *why* alongside *what*.

---

## Current state

**The project is now a fork of [gitlab-review-agent][upstream] (Go, MIT), not a
from-scratch Python build.** That decision was made 2026-08-31 after evaluating
three options; the reasoning is under *Key decisions* below and should not be
relitigated without new evidence.

```
agent/     The reviewer. Go. Forked at upstream ab50d4f, module renamed to
           `habodek` across 58 files. Provenance in agent/NOTICE.md.
eval/      Evaluation harness. Python. 13-case corpus + reviewer-agnostic scorer.
habodek/   The `habodek eval` CLI and its diff-parsing support.
```

Done so far:

- Corpus + scorer built and tested (32 Python tests pass, ruff clean).
- Fork established: source copied, module path renamed, MIT attribution preserved
  in `agent/LICENSE` + `agent/NOTICE.md`, upstream README kept as
  `agent/README.upstream.md`, `upstream` git remote added for future diffing.
- `README.md` and `agent/README.md` written for our setup.

**Verified 2026-08-31:** Go 1.27 installed. `go build ./...` and `go vet ./...` are
clean — the 58-file module rename had no stragglers. `go test ./...` passes for
config, core/reply, core/review, pkg/git and store/file. The **sqlite store tests
fail** because there is no C compiler on this machine, so `CGO_ENABLED=0` and
`go-sqlite3` compiles to a stub. That is an environment gap, not a code defect.

Dead Python removed: the from-scratch reviewer (`habodek/llm/`, `habodek/gitlab/`,
`habodek/agent/`, `habodek/config.py`, `workspace/units.py`) and its 31 tests. The
Python side is now only the evaluation harness — 32 tests, ruff clean.

**Not yet done:** nothing has been run end to end. No review has ever been produced.

---

## The big unblock: everything can be tested locally

Earlier plans assumed the VPN gated all real testing. It does not. This machine has
**32GB RAM, 131GB free on D:, and WSL present**, which is enough to run the entire
stack offline:

| Piece | Local stand-in |
|---|---|
| GitLab | `gitlab/gitlab-ce` in Docker — real API, real webhooks, real MRs |
| Model | Ollama `qwen3:8b` on the RTX 4060 — real tool calling, same family as qwen3.6 |
| Repos under review | The 13 corpus cases, pushed as real MRs |
| Scoring | `habodek eval --from-json` |

So the full loop — webhook fires, agent clones, explores with tools, posts threads,
human replies, agent responds and resolves — is testable **before ever touching the
VPN**. The VPN is then only needed for the real gateway and real repositories.

Caveat that must not be forgotten: **a local 8B model validates plumbing, never
review quality.** Do not tune prompts against qwen3:8b and assume the gains carry to
qwen3.6.

---

## Milestones

- [x] **Corpus & scorer** — 13 cases (13 planted flaws, 2 clean), reviewer-agnostic
      scoring, `habodek eval`. *(2026-08-31)*
- [x] **Build-vs-buy decision** — evaluated PR-Agent and gitlab-review-agent;
      chose to fork the latter. *(2026-08-31)*
- [x] **F1 — Fork established.** Module renamed, attribution, docs. *(2026-08-31)*
- [x] **F2 — It compiles.** Build and vet clean; tests pass except sqlite (CGO).
      *(2026-08-31)*
- [ ] **F3 — Local stack.** Docker Desktop, GitLab CE container, Ollama +
      qwen3:8b. Push corpus cases as MRs. ← **NEXT**
- [ ] **F4 — First real review.** Label an MR `ai-review`, watch it work. Then the
      reply and resolve loop.
- [ ] **F5 — Score and tune.** Run the corpus through it, read noise-on-clean first.
      Adjust `REVIEW_MAX_FINDINGS` and the prompts in `internal/core/prompt/`.
- [ ] **F6 — Deploy.** Real GitLab, real LiteLLM, bot account, webhook secret.
      *(needs VPN)*
- [ ] **F7 — Pilot** on one friendly repo, then widen.

---

## Next up

**F3: stand up the local stack.** In order:

1. `winget install Docker.DockerDesktop` (the WSL backend is already present)
2. GitLab CE container; set the root password, create a scratch project and a bot
   user, note its user id for `GITLAB_BOT_USER_ID`
3. `winget install Ollama.Ollama && ollama pull qwen3:8b && ollama serve`
4. `agent/.env`: `OPENAI_BASE_URL=http://localhost:11434/v1`,
   `LLM_DEFAULT_MODEL=qwen3:8b`, `OPENAI_API_KEY=ollama`, plus the GitLab block
5. `python -m habodek.cli eval --materialise ./cases`, then push each as an MR
6. `go run ./cmd/cli review` first — the interactive dry run is the cheapest way to
   see output without wiring webhooks

---

## Key decisions

| Decision | Why |
|---|---|
| **Fork gitlab-review-agent rather than build, or adopt PR-Agent** | It already does the two things that mattered most: agentic repo exploration (`read_file`, `search_code`, `read_multi_file` — it can look at the caller before commenting) and a replier agent that reads thread history plus surrounding code. PR-Agent is the healthier project (MIT, 10k stars, actively maintained) but is diff-centric — repo context is file inclusion capped at 500 lines, and it has no reply handling. Building from scratch would have reproduced work that already exists under MIT. |
| **Accept that upstream is dormant** | 15 commits, one author, nothing since 2026-04-02, 14 stars. We are the maintainer now. The upside: no upstream means no divergence tax — a fork of a dead project is simply our code. |
| **Go is acceptable despite the team being Python** | Verified by reading the source: pointing it at our gateway is 4 env vars and no code change. Prompts are plain string constants, so editing review behaviour is editing text. The genuinely hard part (worker pool, queue, cron) is competently written with `serverCtx` cancellation and `sync.WaitGroup`, and is code we would not touch. |
| **Module path is bare `habodek`** | Valid Go for a private application, and avoids guessing our GitLab URL. One line in `go.mod` if it is ever published or vendored by path. |
| **Keep the Python eval harness** | It grades *any* reviewer, which is how the fork decision was made and how prompt changes get validated from here. Language-independent by design. |
| **Label trigger, not on-push** | Explicit cost control — `REVIEW_TRIGGER_LABEL`, default `ai-review`. Nothing runs unless somebody asks. Upstream had already made this choice. |
| **Noise on clean cases is the headline metric** | Detection is the number everyone reaches for and the least useful alone: a reviewer that flags everything scores 100% on it. Two corpus cases are clean changes where the correct review is silence. |
| **A corpus hit needs right place *and* right reason** | Location-only matching would credit a comment landing on the correct line while discussing something else, flattering every reviewer. Each expectation carries keywords. |

---

## Gotchas / open questions

**About the fork:**

- **`internal/core/reply/intent.go` classifies replies by English keyword match**
  (`"false positive"`, `"by design"`, ...) and that feeds the self-learning
  feedback loop. **Confirmed non-issue: the team reviews in English.** Revisit only
  if that changes.
- **Context window fallback is 128k** (`resolveContextWindow`) for models absent
  from `defaultContextWindows`. Correct for qwen3.6; a larger model silently
  under-uses its window until a line is added to that map.
- **`max_completion_tokens`**, not `max_tokens`, in `internal/pkg/llm/openai.go`.
  LiteLLM normally translates it; one word to change if a model rejects it.
- **`GITLAB_BOT_USER_ID` is the self-reply loop guard.** Get it wrong and the agent
  talks to itself.
- **No C compiler on this machine, so `CGO_ENABLED=0`.** Two consequences, both
  confirmed: the sqlite store fails at runtime and in tests (`go-sqlite3` is a
  stub), and **`go test -race` refuses to run at all** — the race detector requires
  cgo. Workarounds: use `STORE_DRIVER=file` locally, and run race testing in Docker
  or WSL. To fix properly: `winget install BrechtSanders.WinLibs.POSIX.UCRT` (or any
  mingw-w64), then `CGO_ENABLED=1`.
- **Run `go test -race` before shipping anything concurrent.** Races do not reliably
  fail a normal test run, and this is the one area where Python intuition misleads —
  real parallelism, no GIL. Since it cannot run natively here yet, do it in Docker.
- Go 1.27 is installed; `go.mod` declares 1.25.5. A newer toolchain building an
  older module is fine and expected.
- Upstream history is worthless for archaeology: the first real commit is a single
  "open-source release" dump. `git blame` will not explain much.
- The Go code **has never been compiled here.** The 58-file module rename is
  unverified.

**About the harness:**

- Ruff lints the eval corpus and flags the planted defects; `eval/corpus` is in
  `extend-exclude`.
- Corpus expectations are anchored by substring, not line number — re-run
  `python eval/build_corpus.py` after editing `CASES`.

**Tooling, learned the hard way:**

- **Bash heredocs mangle both regex escaping and `\n`.** Two files were corrupted
  this way. Write Python/Go files with the Write tool, not `cat <<EOF`.

**Open:**

- An adapter for the agent's output format is still needed in `eval/score.py`
  alongside `from_pr_agent()`, so the fork can be scored automatically.
- `habodek/cli.py` needs an `@app.callback()` even though it registers one command:
  without it Typer collapses to a single-command app and rejects `habodek eval` as
  an unexpected argument.

---

## Where things live

```
agent/
  NOTICE.md              Fork provenance + running list of our changes. Update it.
  README.md              Our config: LiteLLM, GitLab, cost controls, maintenance.
  README.upstream.md     Original author's docs — still the best feature reference.
  internal/config/       Every env var; defaultContextWindows lives here.
  internal/pkg/llm/      openai.go (hand-rolled net/http), anthropic, google,
                         balancer, retry. baseURL is a constructor parameter.
  internal/pkg/tools/    read_file, search_code, read_multi_file.
  internal/pkg/gitlab/   discussions, notes, ResolveDiscussion.
  internal/core/prompt/  THE PROMPTS. Plain Go string constants containing
                         markdown — edit as text, no Go needed.
  internal/core/review/  pipeline, planner, risk scoring, chunking, output parsing.
  internal/core/reply/   intent classification + reply pipeline.
  internal/core/agents/  reviewer/ and replier/.
  internal/handler/      webhook, worker pool, cron (feedback consolidation).
  internal/pkg/store/    file / sqlite / postgres.
  cmd/server, cmd/cli    webhook server; interactive dry-run review + clone-all.

eval/
  case.py                Case / ExpectedFinding. materialise() builds a real
                         two-commit git repo per case.
  build_corpus.py        Generates the corpus. Line numbers resolved from an
                         `anchor` substring, so editing a case cannot silently
                         desynchronise its expectations.
  score.py               Reviewer-agnostic scoring. from_pr_agent() adapter;
                         matches() requires right place AND right reason.
  corpus/synthetic/      13 cases. swallowed-exception, lsp-violation and
                         dip-hardcoded-dependency hide the defect OUTSIDE the
                         changed lines — they discriminate context-reading
                         reviewers from diff-only ones.

habodek/
  cli.py                 Entry point: `python -m habodek.cli eval`.
  cli_eval.py            The eval command — list, materialise, score.
  workspace/diff.py      Unified-diff parser. Used by the harness to verify that
                         every planted flaw sits on a line the diff really has.
  workspace/gitcmd.py    git wrapper.
tests/                   32 tests covering the diff parser, scorer and harness.
```

[upstream]: https://github.com/antlss/gitlab-review-agent
