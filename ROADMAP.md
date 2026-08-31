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
local/     The local stack: GitLab CE compose file + the setup runbook.
```

Done so far:

- Corpus + scorer built and tested (36 Python tests pass, ruff clean).
- Fork established: source copied, module path renamed, MIT attribution preserved
  in `agent/LICENSE` + `agent/NOTICE.md`, upstream README kept as
  `agent/README.upstream.md`, `upstream` git remote added for future diffing.
- `README.md` and `agent/README.md` written for our setup; `local/` added with
  the compose file, the runbook and the two provisioning scripts.

**Verified 2026-08-31:** Go 1.27 installed. `go build ./...` and `go vet ./...` are
clean — the 58-file module rename had no stragglers. **`go test ./...` and
`go test -race ./...` both pass across every package**, sqlite included: a
mingw-w64 toolchain is now installed, so `CGO_ENABLED=1` and the earlier CGO gap
is closed. The race detector works natively here — no need for Docker or WSL.

Dead Python removed: the from-scratch reviewer (`habodek/llm/`, `habodek/gitlab/`,
`habodek/agent/`, `habodek/config.py`, `workspace/units.py`) and its 31 tests. The
Python side is now only the evaluation harness — 36 tests, ruff clean.

**F3 done, 2026-08-31.** The whole stack is provisioned on this machine. It is
not currently running — see *Resuming a session* below for how to start it.

- **GitLab CE 19.3.1** in Docker at `http://localhost:8929`, root password in
  `local/.env`. Bot user `habodek-bot` is **user id 2** with an `api` token;
  outbound local requests are enabled so webhooks can reach the host.
- **Ollama 0.33.2** on `localhost:11434` with `qwen3:8b` (4.9GB, stored on `D:`).
- **All 13 corpus cases seeded** as one project + one MR each, every MR is `!1`,
  each with the agent's webhook registered. Project ids are 1-9 and 11-14 —
  `sql-injection` is **14**, because project 10 was destroyed and re-seeded to
  clear the F4 test out of it. Verified that a seeded MR's diff is exactly the
  planted change.
- `agent/.env` is complete and real — no placeholders left. It is gitignored, as
  is `local/.env`; both are on this machine only.

Everything above is reproducible from scratch: `local/README.md` is the runbook,
`local/bootstrap_gitlab.rb` does the user/token/settings, `local/seed_gitlab.py`
does the projects, pushes, MRs and hooks.

**Verified against the live endpoint**, using the exact request body
`internal/pkg/llm/openai.go` builds: HTTP 200, `finish_reason: tool_calls`, a
well-formed `read_file` call with the right arguments. So both open worries about
that file are settled — **Ollama accepts `max_completion_tokens`**, and tool
calling works through the OpenAI-compatible endpoint. First call took 64s cold
(5GB into VRAM); warm calls are far quicker.

**F4 done, 2026-08-31 — the agent has produced real reviews.** Two paths proven:

1. **CLI dry run** on `off-by-one-pagination`: found the planted defect at
   `src/paging.py:4`, correct reason (`end = start + per_page + 1` returns one
   item too many), one finding, no noise. ~87s, two LLM calls.
2. **Webhook** on `sql-injection`: labelling the MR `ai-review` drove the whole
   chain — hook received, job queued, repo cloned, review run, a resolvable
   thread posted on `src/orders.py:8` plus a summary note. ~40s.

**The reply loop works, and is better than expected.** Told "fixed" when nothing
had been fixed, the agent read the code, said the interpolation was still there
and refused to agree. After a real parameterised-query fix was pushed it checked
again and confirmed. It answers from the repository, not from the conversation.

**The F4 evidence has been cleaned up on purpose.** The `sql-injection` project
was destroyed and re-seeded, so its threads, the fix commit and the label are all
gone. All 13 MRs are now uniform — no labels, no comments, one webhook each — so
F5 starts from a baseline that nothing has already reviewed. Do not go looking
for those threads; the findings from them are recorded under *Gotchas* instead.

**Not yet done:** no case has been scored, and the corpus has never been run as
a batch. That is F5.

### Resuming a session

Nothing is running. Bring it back in this order — `local/README.md` has the
detail, this is the short version.

```bash
# 1. GitLab (already provisioned; the volumes hold everything)
cd local && docker compose up -d
curl -o /dev/null -w '%{http_code}
' http://localhost:8929/users/sign_in   # 200 = ready, allow 5 min

# 2. Model
ollama serve            # or launch the tray app; `ollama list` should show qwen3:8b

# 3. Agent
cd agent && go run ./cmd/server                       # webhook listener on :8080
# or, for one MR without webhooks:
go run ./cmd/cli review --project-id 9 --mr-id 1      # 9 = off-by-one-pagination
```

Then trigger a review by labelling: `python local/seed_gitlab.py --label <case>`
(or `--label all`). Nothing runs unless an MR carries `ai-review`.

Two traps worth knowing before you start:

- **`go` is on the machine PATH but not always on a stale shell's.** If it is not
  found, open a new terminal rather than reinstalling anything.
- **GitLab's container health says "healthy" minutes before it serves**, and stays
  healthy through a failed reconfigure. Trust the `curl` above, not `docker ps`.

Credentials are all in gitignored files on this machine: `agent/.env` has the bot
token and `GITLAB_BOT_USER_ID=2`, `local/.env` has the GitLab root password. If
either is ever lost, `docker exec -i habodek-gitlab gitlab-rails runner - <
local/bootstrap_gitlab.rb` reissues the bot token and prints a fresh one.

### Repository

Pushed to `github.com/Noaml22925/Habodek` (`origin`), branch `main`.

History is deliberately structured so the fork delta stays legible:

```
aa92c7f  Add the evaluation harness, and the project docs
1a4254f  Make the fork ours: rename module, add attribution and docs
4989776  Vendor gitlab-review-agent at ab50d4f (MIT)   <- pristine upstream
a72f24a  Initial commit
```

Upstream was committed **untouched first**, so `git show 1a4254f` is exactly "what
we changed". `upstream` is configured as a second remote for the same purpose,
permanently:

```bash
git fetch upstream
git diff upstream/main -- agent/internal/pkg/llm/openai.go
```

`.gitattributes` pins `eol=lf`. Without it a Windows checkout rewrites line endings
on every file and those upstream diffs become unreadable noise.

---

## The big unblock: everything can be tested locally

Earlier plans assumed the VPN gated all real testing. It does not. This machine has
**32GB RAM, 132GB free on D: and an 8GB RTX 4060**, which is enough to run the
entire stack offline:

> This is Windows 11 *Home*, which has no Hyper-V, so Docker Desktop's WSL 2
> backend is the only backend and WSL is a hard prerequisite. Both are installed
> now; a rebuild elsewhere needs an elevated `wsl --install` and a reboot first.

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
- [x] **F2 — It compiles.** Build, vet and the full test suite clean, race
      detector included. *(2026-08-31)*
- [x] **F3 — Local stack.** Docker + WSL 2, GitLab CE 19.3.1 on `localhost:8929`,
      Ollama + qwen3:8b, all 13 corpus cases seeded as MRs with webhooks
      registered. *(2026-08-31)*
- [x] **F4 — First real review.** Both paths work: CLI dry run, and webhook →
      queue → clone → review → threaded comment on the exact diff line. The reply
      loop answers reviewers with repository context. *(2026-08-31)*
- [ ] **F5 — Score and tune.** Run the corpus through it, read noise-on-clean first.
      Adjust `REVIEW_MAX_FINDINGS` and the prompts in `internal/core/prompt/`.
- [ ] **F6 — Deploy.** Real GitLab, real LiteLLM, bot account, webhook secret.
      *(needs VPN)*
- [ ] **F7 — Pilot** on one friendly repo, then widen.

---

## Next up

**F5: score the corpus and tune.** Everything needed is in place — the stack
runs, the scorer has an adapter, the cases are seeded. What is missing is one
script and one judgement call.

1. **Write the collector.** Run all 13 cases and write
   `{case_id: [ParsedComment, ...]}` to a JSON file, then
   `python -m habodek.cli eval --from-json results.json`. Findings come from each
   job's `aiOutputParsed` in `agent/resource/data/review_jobs/`. Either drive it
   through `cmd/cli review` per case, or label all 13 at once
   (`python local/seed_gitlab.py --label all`) and collect afterwards. Budget
   40-90s per case.
2. **Read `clean-extract-method` and `clean-guard-clause` first.** They are the
   two clean cases and the correct review is silence. Noise there is the number
   that decides whether anyone keeps the bot on.
3. **Then the three context cases** — `swallowed-exception`, `lsp-violation`,
   `dip-hardcoded-dependency` — which hide the defect outside the changed lines.
   They are the whole reason this fork was chosen over PR-Agent, so if they miss,
   the build-versus-buy decision deserves a second look.
4. **Tune from what the results show**, not in advance: `REVIEW_MAX_FINDINGS` and
   the prompts in `internal/core/prompt/` (plain Go string constants — edit them
   as text). Remember the caveat: **an 8B model validates plumbing, not review
   quality.** Prompt gains measured against qwen3:8b may not survive qwen3.6.

Two known defects worth fixing while tuning, both written up under *Gotchas*:
comments anchor on the wrong line (the `def`, not the flaw), and auto-resolve
needs an exact line match so a correct fix left its thread open.

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
  from `defaultContextWindows`. **Fixed 2026-08-31:** `LLM_CONTEXT_WINDOWS` takes
  comma-separated `model=size` pairs and is merged over that map, so a new model
  is an env var rather than a code change. Malformed entries warn and are skipped.
  Set it for qwen3.6 at deploy time.
- **`max_completion_tokens`**, not `max_tokens`, in `internal/pkg/llm/openai.go`.
  Accepted by Ollama (verified 2026-08-31). LiteLLM normally translates it too;
  one word to change if the real gateway ever rejects it.
- **`GITLAB_BOT_USER_ID` is the self-reply loop guard.** Get it wrong and the agent
  talks to itself.
- **The CGO gap is closed.** WinLibs mingw-w64 is installed (`go env CC` → its
  `gcc`), `CGO_ENABLED=1`, and both the sqlite store tests and `go test -race`
  pass natively. The old advice to run race testing in Docker no longer applies.
  Local runs still use `STORE_DRIVER=file` — nothing needs sqlite here.
- **Run `go test -race` before shipping anything concurrent.** Races do not reliably
  fail a normal test run, and this is the one area where Python intuition misleads —
  real parallelism, no GIL. It is clean as of 2026-08-31.
- Go 1.27 is installed; `go.mod` declares 1.25.5. A newer toolchain building an
  older module is fine and expected.
- Upstream history is worthless for archaeology: the first real commit is a single
  "open-source release" dump. `git blame` will not explain much.
- **`go run ./cmd/cli review` needs a live GitLab even for a dry run.** It calls
  `GetProject` and `GetMR` before anything else, so it is not a way to try the
  reviewer against a local repo while GitLab is missing.

**About the agent, learned by running it:**

- **Auto-resolve is an exact line match and it missed.** `autoResolveFixedThreads`
  in `internal/core/review/pipeline.go` resolves a bot thread only when the
  thread's file *and exact line* appear in the new diff's added lines. In the
  live test the bot anchored its SQL-injection thread on line 8 (the `def`) while
  the flaw and the fix were on lines 9-10, so a correct fix left the thread open.
  Two separate things to fix, and worth fixing: the model should anchor on the
  offending line, and the match wants a small tolerance window.
- **Resolution never happens on reply, only on re-review.** The reply pipeline
  classifies intent and logs `signal=ACCEPTED`, but nothing in `reply/` calls
  `ResolveDiscussion`. So "reviewer says it is fixed" cannot close a thread; only
  a later review pass can. That signal is sitting there unused if we want it.
- **A push re-triggers a review on its own** (MR `update` event), and the second
  pass on the fixed code correctly posted nothing. Re-labelling afterwards is a
  no-op: `ExistsPendingOrCompleted` dedupes on project+MR+**head SHA** within 30
  minutes, so to force a re-review you need a new commit, not a new label.
- **Two LLM calls per small review** (`iterations=2`, "structured_verify_complete")
  — it drafts, then verifies. Roughly 40-90s per corpus case on qwen3:8b.

**About the local stack:**

- **Ollama's real context window is the binding one, not ours.** It truncates
  past `num_ctx` *without an error*, and `resolveContextWindow` assumes 128k for
  any model absent from its map. That mismatch degrades reviews silently, which is
  the worst way for it to fail. Fixed by `LLM_CONTEXT_WINDOWS` (see below); both
  `OLLAMA_CONTEXT_LENGTH` and `LLM_CONTEXT_WINDOWS` are set to 16384 and **must be
  changed together**.
- **16k is a VRAM budget, not a preference.** qwen3:8b at q4 is ~5.2GB of the
  4060's 8GB; the KV cache for 16k tokens takes most of what is left. Asking for
  more context pushes layers onto the CPU and the review crawls.
- **`OLLAMA_MODELS` is set to `D:\ollama\models`** as a user environment variable,
  because `C:` has ~25GB free and models are 5GB each. It only takes effect for
  processes started *after* it was set, which the first pull was not — the blobs
  landed in `C:\Users\noaml\.ollama` and were moved afterwards. If `ollama list`
  ever comes up unexpectedly empty, suspect that split rather than a lost model.
- **qwen3 is a thinking model and Ollama returns the thinking in a separate
  `reasoning` field**, leaving `content` clean. `openai.go` reads only `content`
  and `tool_calls`, so no reasoning can leak into a posted review — verified, no
  change needed. It does cost tokens though: a trivial one-tool call spent 147
  completion tokens, nearly all of it thinking, against a 16k budget.
- **The container cannot reach the host as `localhost`.** GitLab's webhook URL has
  to be `http://host.docker.internal:8080/webhook/gitlab`.
- GitLab CE is bind-mount-hostile on Windows — the omnibus image chowns its data
  directories on boot and a Windows bind mount cannot express that, so postgres
  refuses to start. `local/docker-compose.yml` uses named volumes and relocates
  Docker's disk image to `D:` instead.

**About the harness:**

- Ruff lints the eval corpus and flags the planted defects; `eval/corpus` is in
  `extend-exclude`.
- Corpus expectations are anchored by substring, not line number — re-run
  `python eval/build_corpus.py` after editing `CASES`.

**Tooling, learned the hard way:**

- **Bash heredocs mangle both regex escaping and `\n`.** Two files were corrupted
  this way. Write Python/Go files with the Write tool, not `cat <<EOF`.
- **`gofmt -l` reports 22 files as unformatted, and they are not.** The working
  tree has CRLF while git stores LF, so gofmt sees every line as changed. Git is
  correct and diffs against upstream are clean; only gofmt is confused. The
  working tree predates `.gitattributes`, so a one-time `git checkout-index -a -f`
  fixes it — **on a clean tree only**, it overwrites uncommitted work. Until then,
  read `gofmt -d` rather than `gofmt -l`: a real complaint shows real hunks.

**Open:**

- ~~An adapter for the agent's output format~~ — **done.** `from_habodek()` in
  `eval/score.py` reads `domain.ParsedComment` and drops suppressed comments;
  `from_json_file()` sniffs the shape per case, so one results file can mix
  reviewers. What is still missing is the *collector*: something to run all 13
  cases and write that file. The findings live in each job's `aiOutputParsed`
  under `agent/resource/data/review_jobs/`.
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
  .env                   Local config, gitignored. Two FILL-ME GitLab values.

local/
  docker-compose.yml     GitLab CE, trimmed to fit beside a model on one machine.
  README.md              The setup runbook: WSL, Docker, bot user, token, webhook.
  seed_gitlab.py         Pushes the 13 cases in as MRs. Idempotent. Labels
                         nothing unless asked, so seeding never triggers a review.
  .env.example           Just the GitLab root password.

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
