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
HANDOFF.md Start here after moving the repo to the VPN machine: what the zip
           left behind, prerequisites, and how to prove it works offline.
DEPLOY.md  The F6 checklist. Every step that needs the VPN, in order.
```

**Local work is finished.** Everything reachable without the VPN has been done;
what remains is `DEPLOY.md`, start to finish, on a machine that can reach the
company GitLab and LiteLLM.

Done so far:

- Corpus + scorer built and tested (38 Python tests pass, ruff clean).
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
Python side is now only the evaluation harness — 38 tests, ruff clean.

**F3 done, 2026-08-31.** The whole stack is provisioned on this machine. See
*Resuming a session* below for how to start it.

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

**F5 scored, 2026-08-31 — the corpus has been run as a batch and graded.**
`local/run_corpus.py` drives `cmd/cli review` once per case, reads each job's
`aiOutputParsed` back out of the store, and writes the file `habodek eval
--from-json` wants. About 60s per case, 12 minutes for all 13.

```
Noise on clean cases   0 (over 2 clean cases)
Precision              100%
Detection              62% (8/13)
Total reported         8
```

**Everything it said was correct, and it said nothing on either clean case.** For
the metric that was declared the headline — noise — this is the best result
available. It reported exactly one finding per case and never more, including on
the two cases with two planted flaws.

> **Read that paragraph with the next section in hand.** Those numbers are one
> sample of a stochastic process, and repeating the run without changing anything
> does not reproduce them. See *What three runs revealed*.

Both of the numbers above were *worse* before the harness was corrected; see
*The scoring fix* below, which is the most important thing in this section.

That run's raw output is still on this machine at `.habodek-work/results.json`,
with each case's CLI log beside it in `.habodek-work/results.logs/`. Gitignored,
so it is here only — re-run the collector rather than hunting for it elsewhere.

The five misses, and what each one means:

| Miss | Why |
|---|---|
| `typos` (2 flaws) | **Declined by policy, not missed.** The extraction prompt bans naming, style and documentation findings, so it returned zero candidates and the verification pass never ran. Whether a typo in a user-facing string is "production impact" is a prompt decision to make at deploy time, not against an 8B model. |
| `lsp-violation` | Genuinely not found. Zero candidates extracted. |
| `n-plus-one-query` | Genuinely not found. Zero candidates extracted. |
| `srp-fat-handler` | Found the *other* planted flaw in that case (plaintext password storage, correctly) and stopped. One finding per review again. |

Two of the three context cases landed: `dip-hardcoded-dependency` and
`swallowed-exception` were both found, `lsp-violation` was not. Those three are
the reason this fork was chosen over PR-Agent, so 2/3 supports the decision
without settling it — worth re-testing on qwen3.6 before drawing a conclusion.

### The scoring fix

The first score read **precision 50%, detection 31%**. That was the harness being
wrong, not the reviewer. Four of the five apparent misses were correct findings
anchored one to three lines away from the planted window:

| Case | Planted at | Reported at | Both are |
|---|---|---|---|
| `sql-injection` | 9, the interpolation | 8, `def find_by_status` | the injected query |
| `mutable-default-argument` | 1, the `def` with `= []` | 4, `events.append` | the shared default |
| `swallowed-exception` | 14-15, the `except` | 11, the `try` | the swallowed error |

`eval/case.py` already said in a comment that the window exists so as not to
"punish reasonable disagreement about whether to flag the `try` or the `except`"
— but the authored windows were too tight to do it. `ANCHOR_TOLERANCE = 3` in
`eval/score.py` now gives that slack uniformly. **The keyword gate is untouched,
so "right reason" is still required and the anti-flattery property survives.** A
uniform documented slack was chosen over hand-widening each case, which would
have tuned the corpus to one reviewer.

The fifth was `off-by-one-pagination`: right file, exact right line, and it still
scored as a miss because the keyword list had `off-by-one` and `overlap` but not
`+1`, which is how the model actually described it. Keywords widened.

Be honest about the risk here: **both changes were made after seeing output, which
is how a corpus gets fitted to its reviewer.** The defence is that neither touches
what counts as the right reason, and that noise-on-clean — the headline — cannot
move either way, because clean cases have no expectations to match against.

### What three runs revealed — read this before trusting any number above

**F5.1, 2026-08-31.** The open anchor defect was root-caused and fixed, and
measuring the fix uncovered something more important than the fix itself.

**The anchor defect was mechanical, not a model failing.** The structured
extract-then-verify path — the one that actually runs — showed the model
`"lineNumber":42` in a JSON example and never said what the field meant. The
legacy prompt path defines it; the structured path did not. A field rule was
added to both `StructuredExtractionSystemPrompt` and `StrictReviewOutputFormat`.

Then the corpus was run twice more. All three runs, same model, same machine:

| | F5 baseline | anchor fix, run 1 | anchor fix, run 2 |
|---|---|---|---|
| Prompts | before | after | **identical to run 1** |
| Noise on clean cases | 0 | 0 | **1** |
| Precision | 100% | 100% | 71% |
| Detection | 62% (8/13) | 46% (6/13) | 38% (5/13) |
| `sql-injection` anchored at | 8 (1 off) | **9 (exact)** | **9 (exact)** |

**Runs 1 and 2 differ only in sampling.** Same prompts, same corpus, same model —
three detections apart, and one produced a false positive on a clean case while
the other did not. Reviews run at `Temperature: 0.1` with no seed
(`internal/core/review/structured_review.go`), and qwen3 is a thinking model, so
small divergences in a long sampled reasoning chain compound.

The consequences are worth stating plainly:

- **F5's headline numbers are one sample, not a property of the reviewer.** The
  perfect noise-on-clean of 0 did not survive a repeat of the same
  configuration. Noise-on-clean is still the right metric — it is the second run
  that says so, by catching a false positive on `clean-guard-clause` ("error
  message mismatch") that the first two runs missed entirely.
- **Run-to-run variance is larger than any prompt change made so far.** So no
  single-run A/B on this rig can attribute anything, and the drop from 62% to
  46% must *not* be read as damage done by the prompt edit.

**Anchoring, unlike detection, turned out to be stable.** Of the five findings
reported in both the baseline and run 1, four landed on the byte-identical line
and one moved — `sql-injection`, from the `def` on line 8 to the interpolated
query on line 9, exactly where the flaw is. It then stayed on line 9 in run 2.
That is the change doing what it was written to do, visible precisely because
anchoring does not wander the way detection does.

Two cases still anchor three lines off (`mutable-default-argument`,
`swallowed-exception`), so the instruction is a partial lever. Its wording was
revised once on reasoning rather than on scores: the first version said "not the
enclosing function signature", which is wrong for a mutable default argument —
that defect *is* on the signature. It now reads "the line that would have to
change to fix the issue". **It deliberately carries no examples**, because the
obvious ones would have been lifted from our own corpus, and a prompt that names
its test set is a prompt fitted to it.

**Nothing was dropped by the new rule.** Every regressed case logged
`suppressed: 0` — the extraction pass returned no candidates, which is the same
failure mode as the baseline's own misses. `output_parser.go` does discard a
finding whose line is not in the diff (`DropReason = "invalid_line"`), so this
was the mechanism worth ruling out, and it was ruled out.

### What this means for F6, concretely

**Do not A/B a prompt change on a single run — not here, and not on the real
gateway, where each run also costs money.** Two ways out, and they answer
different questions:

- **N≥3 runs per configuration.** This is the one that actually works, and it is
  not optional. Compare distributions, never single numbers.
- **`LLM_SEED`** (added 2026-08-31, off by default) reduces variance but,
  **measured against Ollama, does not deliver reproducible reviews.** It is worth
  setting and worth re-testing on the real gateway; it is not a substitute for
  the line above. The evidence is in the next section.

### What `LLM_SEED` actually buys, measured

Tested rather than assumed, because `DEPLOY.md` leans on it:

- **The plumbing is correct.** With `LLM_SEED=1234` the request body grows by
  exactly 12 bytes — `,"seed":1234` — on every call. The seed reaches the wire.
- **Ollama honours it on its native API**: four calls at temperature 0.8
  returned byte-identical completions.
- **The OpenAI-compatible `/v1` endpoint is weaker**: three of four identical.
- **A full review is not reproducible.** Two runs of `sql-injection` with the
  same seed produced differently worded findings — both correct, both anchored
  on line 9. A review is several LLM calls deep with tool use in between, and
  llama.cpp is not bit-deterministic across runs regardless of seed.

So the seed narrows the spread and does not close it. **Do not read a single
seeded run as reproducible.** The real gateway is likely a different engine
(vLLM behind LiteLLM is friendlier to determinism than llama.cpp), so re-run
this same check there rather than inheriting the local answer.

The raw material for all three runs is on this machine, gitignored:
`.habodek-work/results-f5-baseline.json`, `results-anchor.json`,
`results-anchor-run2.json`, each with per-case CLI logs beside it.

### Resuming a session

GitLab and Ollama were left **running** at the end of the F5 session, but do not
count on it — a reboot ends both. Every command below is safe to run against an
already-running stack, so just run them. `local/README.md` has the detail; this
is the short version.

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

To score the whole corpus instead — no server, no labels, nothing posted:

```bash
python local/run_corpus.py --keep-logs        # ~12 min for 13 cases
python -m habodek.cli eval --from-json .habodek-work/results.json --verbose
```

This is the F5 loop and it is worth repeating whenever the prompts or the model
change. It never posts and never labels, so the corpus stays uniform.

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
4989776  Vendor gitlab-review-agent at ab50d4f (MIT)   <- pristine upstream
1a4254f  Make the fork ours: rename module, add attribution and docs
         ... our work continues on top
```

Upstream was committed **untouched first**, so everything after `4989776` is
exactly "what we changed" — `git log --oneline 4989776..HEAD` is the whole delta,
and `agent/NOTICE.md` carries the same list in prose, with reasons. `upstream` is configured as a second remote for the same purpose,
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
- [x] **F5 — Scored.** All 13 cases run as a batch and graded: noise 0, precision
      100%, detection 62%. **Tuning was assessed and deliberately not done** — see
      *Next up* for why that is a decision, not an omission. *(2026-08-31)*
- [x] **F5.1 — The anchor defect closed, and the rig's limits found.** Root cause
      was a prompt field never defined, not the model. Two further runs showed
      run-to-run variance exceeds any prompt change we have made, so single-run
      A/B on this rig proves nothing; `LLM_SEED` was added to fix that. Anchor
      distance is now measured. *(2026-08-31)*
- [ ] **F6 — Deploy.** Real GitLab, real LiteLLM, bot account, webhook secret.
      **Work from `DEPLOY.md`**, which is the whole checklist. *(needs VPN)*
- [ ] **F7 — Pilot** on one friendly repo, then widen.

---

## Next up

**F6: deploy. Everything else is done.** All local work is finished — there is
nothing left that can be attempted without the VPN, and `DEPLOY.md` is the
checklist for what remains, written to be worked through top to bottom.

**There is nothing further worth measuring on this machine**, and F5.1 sharpened
why: run-to-run variance here is larger than the effect of any prompt change, so
another local run buys a number that the next local run will contradict.

**Why tuning stopped rather than continued.** The obvious levers were checked and
each one is a bad trade against this model:

- `REVIEW_MAX_FINDINGS` is set to 10 and **is not binding** — the agent reported
  one finding per case. Moving it in either direction changes nothing.
- The remaining detection misses come from the *extraction* pass returning zero
  candidates, not from verification rejecting them. The instruction producing that
  is `Prefer false negatives over false positives` in
  `StructuredExtractionSystemPrompt`. Relaxing it is precisely the trade of
  noise-on-clean — the declared headline metric, currently a perfect 0 — for
  detection.
- Making that trade against qwen3:8b would be measuring the wrong thing. The
  standing caveat applies at full force: **an 8B model validates plumbing, not
  review quality.** Prompt gains here may not survive qwen3.6, and a regression in
  noise very likely would.
- **And, since F5.1, a fourth reason that applies to all of them:** the rig
  cannot tell a real gain from sampling noise. Two runs of an identical
  configuration differed by three detections and by one false positive on a
  clean case. Any local A/B would be reading its own variance.

So the quality prompts are unchanged and deliberately so. The one prompt edit
that was made — defining `lineNumber` — is not in this category: it fixes where a
finding is attached, not what counts as a finding, and it cannot make the
reviewer say more or less than it otherwise would.

**Re-run `local/run_corpus.py` against the real gateway once the VPN is
available** — that run, not this one, is the one worth tuning against. Set
`LLM_SEED` first, and run each configuration more than once. `DEPLOY.md` step 7
has the loop and the levers in order.

What F6 needs: real GitLab, real LiteLLM, a bot account and webhook secret, and
`LLM_CONTEXT_WINDOWS` set for qwen3.6 (see *Gotchas*).

The wrong-line anchoring defect is **fixed, partially** — see *What three runs
revealed*. `sql-injection` now anchors exactly on the flaw and stayed there
across two runs; `mutable-default-argument` and `swallowed-exception` are still
three lines off. Check `Anchored on the flaw` in the eval output against the real
model before deciding whether more is needed; the remaining two may simply be an
8B model's reading of a construct.

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
| **A single corpus run is not evidence** *(2026-08-31)* | Two runs of an identical configuration differed by three detections, and one raised a false positive on a clean case while the other raised none. Reviews sample at temperature 0.1 with no seed, and a thinking model compounds small divergences. So any claim of the form "this change improved X" needs either `LLM_SEED` pinned, or several runs — and F5's headline numbers are one draw, not a property of the reviewer. |
| **Anchor distance is measured, but is not a headline number** *(2026-08-31)* | `ANCHOR_TOLERANCE` deliberately scores a correct finding on a neighbouring line as a hit, which is right — and which hides a real presentation defect. Splitting "was it right" from "was it attached in the right place" keeps both visible without letting either distort the other. |
| **The `lineNumber` prompt rule carries no examples** *(2026-08-31)* | The natural examples were the three corpus cases where the drift was observed. A prompt that names the cases it is scored on is fitted to its test set, and would have made the corpus useless for exactly the comparison it exists to support. The rule is stated generically instead. |
| **"Right place" carries three lines of slack** *(2026-08-31)* | Reviewers anchor the same defect on different lines of the same construct — the `def` or the query inside it, the signature or the `append`, the `try` or the `except` — and none of those readings is wrong. Without the slack the first F5 run scored 50% precision instead of 100%, entirely on findings that were correct. The keyword gate is untouched, so this buys nothing for a reviewer that comments on the wrong thing, and noise-on-clean cannot move at all. |

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
- **`LLM_SEED` narrows sampling variance but does not remove it; unset means the
  field is omitted.** *(Added and measured 2026-08-31.)* `OpenAIClient.WithSeed`
  puts `seed` in the request body only when the env var is set, so a gateway that
  does not support the parameter never sees it and production behaviour is
  unchanged by default. **openai provider only** — the anthropic and google
  clients ignore it. Verified to reach the wire (+12 bytes, exactly
  `,"seed":1234`) and verified *not* to make a full review reproducible on
  Ollama — see *What `LLM_SEED` actually buys*. Set it for measurement runs and
  still run them more than once; leave it unset in production, where varied
  sampling costs nothing.
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

- **Auto-resolve needed a tolerance window. Fixed 2026-08-31.**
  `autoResolveFixedThreads` in `internal/core/review/pipeline.go` used to resolve
  a bot thread only when the thread's file *and exact line* appeared in the new
  diff's added lines. In the live test the bot anchored its SQL-injection thread
  on line 8 (the `def`) while the flaw and the fix were on lines 9-10, so a
  correct fix left the thread open — the bot insisting a fixed thing is still
  broken, which is how a reviewer stops trusting it. `nearModifiedLine` now
  allows `resolveAnchorTolerance = 3`, with a test.
- **The other half of that defect had a mechanical cause, not a model one.**
  *(Root-caused and fixed 2026-08-31.)* The model anchored on the `def` rather
  than the flaw — line 8 for a flaw on 9, line 11 for a flaw on 14 — because
  **nothing ever told it what `lineNumber` meant.** The legacy prompt path
  defines the field (`system_prompts.go`: "the specific line in the NEW version
  of the file where the issue is located"); the structured extract-then-verify
  path, which is the one actually running, showed `"lineNumber":42` in a JSON
  example and defined it nowhere. A field rule was added to both
  `StructuredExtractionSystemPrompt` and `StrictReviewOutputFormat`. See
  *The anchor fix* under *Current state* for what it measured.
- **A finding on a line the diff did not add is dropped, not moved.**
  `output_parser.go` sets `DropReason = "invalid_line"` when `lineNumber` is
  absent from that file's added lines. So the anchor instruction cannot cause a
  finding to land somewhere illegal — the worst case is that a correct finding
  is discarded, which shows up as a detection loss, not as a bad comment.
- **Resolution never happens on reply, only on re-review — and that is correct.**
  *(Investigated and closed 2026-08-31; the earlier note called this a gap and
  was wrong on both halves.)* Nothing in `reply/` calls `ResolveDiscussion`, by
  design: upstream has an explicit test,
  `TestPipelineExecuteDoesNotAutoResolveAcknowledgeOrRejectReplies`, asserting it
  does not. A developer saying "fixed" moves the thread to
  `pending_verification`, not resolved — the agent verifies against the
  repository on the next pass instead of taking their word for it. That is
  exactly the behaviour that impressed in F4, where it was told "fixed", read the
  code, and refused to agree. Wiring resolve-on-reply would destroy it.
- **And the reply signal is not unused.** It reaches the next review: rejected
  and accepted feedback is stored, then re-injected by
  `StructuredPromptContext` as `Recent developer feedback: - [rejected] ...`,
  which lands in both the extraction prompt and the reviewer agent. The
  self-learning loop is closed end to end.
- **The one genuinely write-only value is `ThreadState`.** `acknowledged`,
  `pending_verification` and `dismissed` are computed by `ComputeThreadState`,
  persisted, and read by nothing. `autoResolveFixedThreads` decides purely on
  "was a line near the anchor touched", ignoring thread state. Gating it on
  `pending_verification` was considered and rejected: most fixes are pushed
  silently without a reply, so that would stop resolving the common case to
  tidy up a rare one.
- **A push re-triggers a review on its own** (MR `update` event), and the second
  pass on the fixed code correctly posted nothing. Re-labelling afterwards is a
  no-op: `ExistsPendingOrCompleted` dedupes on project+MR+**head SHA** within 30
  minutes, so to force a re-review you need a new commit, not a new label.
- **Two LLM calls per small review** (`iterations=2`, "structured_verify_complete")
  — it drafts, then verifies. Roughly 40-90s per corpus case on qwen3:8b. **A
  review that finds nothing costs only one call**: extraction returns
  `{"candidates":[]}` and the verification pass is skipped, so the clean cases
  come back in about 30s.
- **It reports at most one finding per review**, on every case in the F5 batch,
  including the two cases with two planted flaws. This is not a cap —
  `REVIEW_MAX_FINDINGS` is 10 and never binds — it is what the extraction pass
  returns. Re-check on qwen3.6 before treating it as a property of the agent
  rather than of an 8B model.
- **A review that finds nothing writes `null`, not `[]`.** `AIOutputParsed` is a
  Go `[]byte` holding a marshalled nil slice, and it is stored base64-encoded
  because that is how `encoding/json` writes `[]byte`. Anything reading job
  output has to decode base64 *and* treat `null` as "no findings" — otherwise the
  correct answer on a clean case looks like a crash. `local/run_corpus.py` gets
  this right; it cost two debugging rounds to learn.

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
  `python eval/build_corpus.py` after editing `CASES`. It is deterministic: after
  a keyword edit, only that case's `case.yaml` should show in `git diff`.
- **Anchor distance is reported but is not a headline number.** `anchor_distance()`
  in `eval/score.py` measures how far outside the planted window a *matched*
  finding landed; `habodek eval` shows it as "Anchored on the flaw: n/m (mean x
  lines off)", and `--verbose` marks each drifted hit. It exists because
  `ANCHOR_TOLERANCE` hides exactly the defect above: a thread on the wrong line
  scores as a clean hit. This separates "was it right" from "was it attached in
  the right place", which are different questions and should never share a
  number. Reviewer-agnostic, so it stays useful against the real model.
- **`ANCHOR_TOLERANCE = 3` in `eval/score.py` is load-bearing.** Without it the
  same run scores 50% precision instead of 100%, because correct findings landing
  on the `def` rather than the flaw are counted as noise. If you ever tighten it,
  expect the headline numbers to move for reasons that have nothing to do with
  the reviewer.
- **The console here is cp1252.** A `→` or `—` in anything the harness *prints*
  raises `UnicodeEncodeError` and kills a script at the last line, after the
  expensive part has already run. Keep script output ASCII; source comments and
  Markdown are fine.

**Tooling, learned the hard way:**

- **Bash heredocs mangle both regex escaping and `\n`.** Two files were corrupted
  this way. Write Python/Go files with the Write tool, not `cat <<EOF`.
- **The CRLF working tree is fixed, and `gofmt -l` is a real signal again.**
  *(2026-08-31.)* The tree predated `.gitattributes`, so gofmt saw every line of
  22 files as changed. `git checkout-index -a -f` does **not** fix it —
  `core.autocrlf=true` is set globally on this machine and wins. What works is
  deleting the tracked files and checking them out again, which applies the
  `eol=lf` attribute:

  ```bash
  git status --porcelain     # MUST be empty; this destroys uncommitted work
  git ls-files -z | xargs -0 rm -f
  git checkout -- .
  ```

  That took `gofmt -l` from 22 files to 6 genuinely unformatted ones — import
  ordering in upstream files, masked all along by the noise — and those were
  then formatted. **`gofmt -l` now returns nothing, so it is a usable signal
  again**; the old advice to read `gofmt -d` instead is retired. The six were
  upstream files, so this adds a small formatting-only delta against upstream,
  accepted deliberately in exchange for a lint that works.
- **The corpus generator used to undo that fix on every run.**
  `build_corpus.py` wrote with `write_text(..., encoding="utf-8")`, which on
  Windows translates to CRLF, so a rebuild — or just running the test suite —
  rewrote all 41 corpus files and dirtied the tree. Both call sites now pass
  `newline="\n"`. This is what makes the generator's real property hold here:
  after a keyword edit, exactly one `case.yaml` shows in `git diff`.

**Open:**

- ~~An adapter for the agent's output format~~ and ~~the collector~~ — **both
  done.** `from_habodek()` in `eval/score.py` reads `domain.ParsedComment` and
  drops suppressed comments, `from_json_file()` sniffs the shape per case so one
  results file can mix reviewers, and `local/run_corpus.py` produces that file
  for all 13 cases in one command.
- **Should `typos` stay in the corpus?** The agent declines it by policy — the
  extraction prompt bans naming, style and documentation findings, so it returns
  zero candidates rather than missing anything. As written the case scores two
  permanent misses against a deliberate design choice, which makes detection read
  low for the wrong reason. Either widen the allowed categories at deploy time
  (a typo in a user-facing string arguably *is* production impact) or move the
  case to `tolerate`. Decide it against the real model, not qwen3:8b.
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

HANDOFF.md               The first thing to read on the VPN machine: what the
                         zip did not carry (both .env files), prerequisites,
                         the Windows line-ending step, and the offline checks
                         that must pass before touching the gateway.

DEPLOY.md                The F6 checklist: bot account, config, webhook, the
                         three gateway unknowns and how to test each, then the
                         scoring loop and which prompt levers to pull once the
                         numbers come from the real model. Written to be worked
                         through top to bottom on the VPN.

local/
  docker-compose.yml     GitLab CE, trimmed to fit beside a model on one machine.
  README.md              The setup runbook: WSL, Docker, bot user, token, webhook.
  seed_gitlab.py         Pushes the 13 cases in as MRs. Idempotent. Labels
                         nothing unless asked, so seeding never triggers a review.
  run_corpus.py          THE COLLECTOR. Runs all 13 through `cmd/cli review` and
                         writes the JSON `habodek eval --from-json` scores.
                         Resolves project ids from GitLab rather than hardcoding
                         them, writes results after every case so an interrupt
                         costs only what is left, and `--only` reruns one case.
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
tests/                   38 tests covering the diff parser, scorer and harness.
```

[upstream]: https://github.com/antlss/gitlab-review-agent
