# Habodek reviewer v2 — design proposal

**Status: direction approved 2026-11-24; implementation starts from
[`reviewer-v2-handoff.md`](reviewer-v2-handoff.md).** Written 2026-11-24 against
`17ca1f6`. Survey evidence is in [`reviewer-v2/survey/`](reviewer-v2/survey/README.md).

> **The owner's answers to §12 changed parts of this design, and the handoff wins
> where the two differ.** In short:
> - No gateway from development sessions; the owner tests on the VPN and can paste
>   back only short output.
> - Python first (C/C++ later, low priority).
> - Internal libraries come from the venv and Artifactory, not from GitLab tags.
> - Linux and Docker on the VPN host.
> - **About 25 requests per minute** on the gateway, which makes requests, not
>   tokens, the scarce resource.
> - The Qwen licence is no concern.
>
> Affected: §4.3 (how the models get tested), §6.3 (dependency source), §6.4 (tier 2
> is jedi), §6.6 (fewer finders, sequential verification, request budgets), §6.11
> (sandbox = sibling containers), §7 (delivery), §8 (time), §10 (plan), §12
> (answered).

The target, from the brief: a background reviewer whose thinking is equivalent to
opening Claude Code on the repository and asking "review this MR and post
comments" — and, when the developer answers, checking the fix or the claim the way
Claude Code would: go to the code, verify, change its mind only on evidence.

---

## 0. Summary

- **The reviewer is not short of model; it is structurally prevented from
  looking.** It sees the changed files through ±12-line windows, is told not to
  look further, cannot search for a caller, gets verification evidence truncated
  to the first 60 lines of each file, throws away its own reasoning on every call,
  and re-checks fixes by reading ±30 lines at a stale line number. Details in §2,
  every point cited.
- **Rebuild the brain, keep the control plane.** The Go service that receives
  webhooks, queues jobs, talks to GitLab and fetches git over the VPN stays and
  gets smaller. Everything that thinks is replaced by a new Python program run per
  job (§6).
- **The review is a fixed workflow in code, not a prompt:** understand the MR →
  ~10 finders in parallel, each with a different lens, producing *hypotheses* →
  one investigator agent per hypothesis → mechanical evidence checks (every quoted
  line must exist; the anchor must be on a changed line) → two adversarial
  verifiers per finding, one per model → judge and write. Only what survives is
  posted. Failure is closed. (§6.6)
- **Our own small agent loop**, with its hard parts — reasoning replay, tool-call
  repair, compaction, loop guards — taken from MIT/Apache code that already copes
  with these two models (OpenHands, Hermes, Goose, Qwen Code). Qwen Code headless
  and the OpenHands SDK run the same roles in the first build step as measured
  challengers; if either beats ours, we switch. (§5)
- **Real code exploration:** ripgrep and tree-sitter (definitions, references,
  callers, change map, repo map) everywhere; language servers where we can ship
  them; git history; other repositories at the right version; and sandboxed
  builds and tests, the strongest evidence there is. (§6.3, §6.4)
- **The second half becomes one thread agent with tools.** It starts from the
  stored finding and its evidence chain, maps every line through the diff since,
  goes where the developer points, checks every site and not just the anchored
  line, and has its conclusion attacked by the other model before posting. (§6.7)
- **Memory per repository** (a maintained repo card, a team-owned `.habodek.md`,
  lessons from *verified* thread outcomes), **a full trace of every run** with an
  offline viewer, replay and a "probe this hypothesis" command. (§6.8, §6.9)
- **Models: not tested, because this environment cannot reach the gateway** — that
  is the first of the questions in §12. What can be known from the published chat
  templates is certain and is designed in (reasoning echoed on every turn, no
  mid-conversation system messages, vendor sampling). Which model does what is a
  stated hypothesis, decided by a day-one probe and the scenarios. The Qwen
  model's licence needs a legal read. (§4)
- **Time:** a typical MR is estimated at 20-35 minutes, a reply 3-8 minutes —
  estimates until the gateway is measured. (§8)
- **First build step after approval:** R0, a gateway probe; then R1, the riskiest
  bet in isolation — can a flash model hold a cross-repository investigation, and
  can the other model refute its wrong findings, on four scenarios. Nothing else
  is built until that passes. (§10)

---

## 1. Where the sources and the code differ from the brief and the documents

- **The Anthropic review recipes never run anything.** The brief calls a test,
  type-check or lint run in a sandbox the strongest evidence there is, and this
  design agrees (§6.4). But the recipes the brief points to for ideas — the
  code-review plugin, claude-code-security-review and its successor
  security-guidance — never execute code, and several forbid it ("do not run the
  linter to verify"; "Do not use the bash tool") (survey, `anthropic-review.md`
  §4). Their strength is reading beyond the diff by procedure and verifying each
  claim independently. So execution is where we go beyond them, not where we copy
  them: for a weaker model, an executed check is the one kind of evidence that does
  not rest on its judgement.
- **The offline build from a git clone is broken.** The root `.gitignore` is the
  Python template and ignores `lib/` (`.gitignore:17`), which silently drops
  `agent/vendor/github.com/lib/pq` from git. `vendor/modules.txt` still lists it,
  so `go build` from a clone fails with `cannot find module providing package
  github.com/lib/pq`. It only works on the VPN machine if the tree went across by
  zip rather than clone.
- **`go vet` and `go test ./...` fail at HEAD.** `17ca1f6` changed
  `ReplyJobStore.UpdateCompleted` and did not update
  `internal/pkg/store/file/job_store_test.go:283`. Every other package passes.
- **`ROADMAP.md` predated `17ca1f6`.** That commit replaced auto-resolve with
  posted verdicts (`internal/core/review/verdict.go`); `DEPLOY.md` described the new
  behaviour, the roadmap the old one. Fixed in this change.

The build and test problems are one-line fixes, left for milestone M0 of the
handoff, which also lists a third: a Python test that fails on Linux because of a
Windows-only path assumption.

---

## 2. Diagnosis: where the current reviewer loses context and reasoning

Read in the code at `17ca1f6`; paths are relative to `agent/`.

### 2.1 The brief, checked

| Claim in the brief | Verdict | Evidence |
|---|---|---|
| The running review is two single LLM calls, no tool loop | ✔ | `executeSingle` and `executeChunked` both call `runStructuredChunkReview` (`internal/core/review/pipeline.go:313,380`), which is one extraction call and one verification call (`structured_review.go:117,160`). |
| Extraction sees only diff + changed files; full file up to 400 lines, else ±12 | ✔, and worse | `bundleFullFileMaxLines = 400`, `extractionExcerptRadius = 12` (`structured_review.go:21-22`). Also at most **4** windows per file (`:23`, enforced at `:303-305`): in a large changed file, changes after the fourth window are not shown at all. The diff is compacted to 2 context lines (`:199`). |
| The prompt says "Only analyze changed code shown in the bundle" and "Prefer false negatives" | ✔ | `internal/core/prompt/structured_review_prompts.go:21,29`. |
| Verification gets evidence gathered once, from one model request; no follow-ups | ✔, and worse | Requests from all candidates are pooled and capped at 12 paths / 8 symbols / 8 patterns (`structured_review.go:27-29,696-727`) and executed mechanically (`:645-694`). Each path comes back through `read_multi_file`, which keeps the **first** `TOOL_RESULT_MAX_LINES` lines of the file (default 60, `internal/config/config.go:222`; `internal/pkg/tools/tools.go:290-293`). Asking for the contract in a file returns its imports. |
| A real 9-tool agent loop exists and nobody calls it | ✔ | Built in DI (`internal/di/core.go:26-27`), stored on the pipeline (`pipeline.go:32,59`), never run. |
| That loop is limited: 4096 tokens, crude compaction | ✔, and broken | `agentMaxTokens = 4096`, `compressKeepRecent = 4` (`internal/core/agents/reviewer/agent.go:17-18`). Compaction keeps `rest[len(rest)-3:]` (`:325-326`); after a turn with several tool calls that is a run of `tool` messages whose `assistant` message was dropped — an invalid sequence strict servers reject. Its posture is "stop exploring" (`internal/core/prompt/user_prompts.go:19-35`). |
| Reply and verdict are single calls, ±20 / ±30 lines, 600 tokens | ✔, and worse | `replyMaxTokens = 600` (`internal/core/agents/replier/agent.go:14`); ±20 (`internal/core/reply/pipeline.go:252-253`); ±30 (`internal/core/review/verdict.go:17`). See 2.3. |
| Many limits were built for a small, short-context model | ✔ | Sized for qwen3:8b at 16k context on an 8 GB GPU (ROADMAP, *About the local stack*), never revisited for the gateway models. |

### 2.2 The review

1. **It cannot look.** The extraction prompt forbids leaving the bundle, and the
   bundle is only the changed files. The only route outward is the pooled evidence
   request, answered once, blind, truncated from the top. There is no way to ask
   "who calls this?": `search_code` rejects `( ) { } $ | < >` in a pattern
   (`tools.go:193-197`), so `Foo(` cannot be searched for, and results are capped
   at 20 lines in total (`config.go:217`). `get_symbol_definition` finds
   definitions by regex; nothing finds references.
2. **Then it is told to drop what it could not verify.** "If evidence is
   inconclusive, drop the candidate" (`structured_review_prompts.go:51`), with
   evidence fetched without knowing what to look for. LOW-confidence candidates die
   before verification (`structured_review.go:550`), and at most 8 survive
   extraction (`:26,580`). This is the mechanism behind "one finding per review"
   and the zero-candidate misses in F5 — not only the 8B model.
3. **Cross-file bugs inside the same MR are split apart.** 21-80 changed files are
   reviewed as independent chunks of 10 (`internal/core/review/chunk.go`,
   `pipeline.go:345`), each blind to the others. At 81+ files, "triage" keeps the
   30 riskiest files and reviews them in *one* call (`planner.go`;
   `pipeline.go:164-170` falls through to `executeSingle`).
4. **Consequences elsewhere cannot be posted.** A candidate must sit on an added
   line (`structured_review.go:559`; `output_parser.go:204-209`,
   `DropReason = "invalid_line"`). Anchoring on a changed line is right for GitLab,
   but nothing tells the model to anchor on the *cause* and cite the broken
   consumer in the body, and extraction is forbidden from finding the consumer.
5. **Other repositories do not exist.** One project is cloned
   (`internal/pkg/git/manager.go`); the tool registry is rooted in it
   (`internal/pkg/tools/registry.go:48`). A library in another repo or from the
   internal registry cannot be read at any version.
6. **Thinking is discarded and sampled near-greedy.** `openai.go` neither reads nor
   sends back `reasoning_content` (`internal/pkg/llm/openai.go:81-100,156-178`),
   sets no thinking controls, and reviews run at `Temperature: 0.1`
   (`structured_review.go:110,153`). Both gateway models' templates expect their
   earlier reasoning back on every turn and their vendors publish temperature 1.0
   for thinking mode (§4).
7. **No hypotheses, no follow-up, no change of mind.** The structure is *generate →
   filter → one-shot check*. Nothing in it can notice that a candidate is wrong
   because of something found while checking another, or that a check raised a
   new question.

### 2.3 The second half

1. **No tools.** Both reply and verdict are one call (`replier/agent.go:88-104`).
   "This is handled in the middleware" cannot be checked: the middleware is not in
   the prompt and nothing can fetch it.
2. **It reads the wrong place.** The anchor is `firstNote.Position.NewLine`
   (`internal/handler/webhook/handler.go:279`; `context_gatherer.go:141`); the
   position's SHAs are not even parsed (`internal/domain/model.go:252-256`). GitLab
   keeps a diff note's position current only while its line survives a push; once
   the developer edits that line, the position refers to the old revision. The
   re-check then reads ±30 or ±20 lines around that old number in the new HEAD
   (`verdict.go:155-182`, `reply/pipeline.go:246-266`) — misaligned in exactly the
   case where the developer changed the code.
3. **Thinking models will often say nothing.** `max_completion_tokens: 600` covers
   reasoning *and* answer. When reasoning uses it up, content is empty: the verdict
   pass posts nothing (`verdict.go:88-91`) and the reply job fails
   (`reply/pipeline.go:131-133`). Not measurable from here; R0 checks it.
4. **A fix in another file never triggers a re-check.** `needsVerdict` fires only
   if the thread's own file changed or someone replied (`verdict.go:151-153`). The
   common fix — add the guard in the caller — leaves the thread unchecked.
5. **"Did the fix cover every case?" is unanswerable.** The finding is stored as a
   200-character summary (`pipeline.go:228`); its reasoning, the callers it was
   about and the head SHA it was made at are gone.
6. **A reply and a re-check can both answer the same thread** after a push plus a
   "fixed"; nothing coordinates them.

### 2.4 Memory, observability, noise

- **Memory** is the five most recent feedback one-liners (`context_gatherer.go:65`)
  plus a weekly cron that rewrites a custom prompt from them
  (`internal/core/feedback/consolidator.go`). Nothing about the repository itself —
  layout, layering, invariants, how to build it. Every review starts from zero.
- **Observability** is the final text, the parsed comments, an iteration count and
  a token estimate (`internal/domain/model.go:55-58`). No messages, reasoning or
  tool calls. Candidates dropped at extraction (`structured_review.go:544-569`) are
  not recorded anywhere. Nothing can be replayed.
- **Noise control** is post-hoc string filtering — drop if the comment ends in `?`
  or contains "please check" (`output_parser.go:318-344`), a category allow-list,
  LOW dropped. It removes phrasing, not wrong claims.
- **The queue is in memory** (`internal/pkg/queue/queue.go`); a restart drops queued
  jobs. Tolerable for 60-second reviews, not for 30-minute ones.

---

## 3. What Claude Code does that Habodek does not

Most of the gap is harness, not model:

| When Claude Code reviews a repo, it… | Habodek today | What closes it |
|---|---|---|
| reads the changed files in full, and whatever else it decides it needs | ±12 lines, told not to look further | tools without the old caps, and a workflow that requires reading (§6.4, §6.6) |
| asks "who calls this?" and follows it across packages | cannot search for `Foo(`; no references | `references`, `callers`, change map (§6.4) |
| forms a suspicion, checks it, then checks what the check raised | one blind evidence fetch | an investigator per hypothesis, with a budget (§6.6) |
| changes its mind when the code disagrees | "if inconclusive, drop" | refuted is a recorded outcome with evidence (§6.6, §6.9) |
| keeps its reasoning across steps | discarded every call | reasoning echoed per model (§4, §6.5) |
| uses history: blame, log, the commit that last touched the line | none | `git_log`, `git_blame`, a history lens |
| runs the build or a test when that settles it | never | sandboxed `run` (§6.4, §6.11) |
| knows the project from earlier work (CLAUDE.md, memory) | starts from zero | repo memory (§6.8) |
| when told "fixed", reads the fix and everything it should have covered | ±20 lines at a stale line | the thread agent (§6.7) |

Two things on that list are model-bound: how good the hypotheses are, and how
reliably a model follows a 40-step investigation. The rest is harness. So the plan
is to build the harness properly and then measure the models inside it.

---

## 4. The models

**Nothing in this section has been run against our gateway**; this environment
cannot reach it (§12, question 1). What follows is what can be established from
the models' published chat templates and serving recipes (read from the copies in
the vLLM and SGLang repositories), from LiteLLM's source, and from issue trackers
— survey, `models.md`. Enough to design the harness correctly and to write the
test that settles the rest. Not enough to pick a winner, and I have not.

### 4.1 What the names most likely are

| | "qwen3.8 flash next" | "GLM5.3 flash" |
|---|---|---|
| Probably | `Qwen/Qwen3.8-Flash-Next` (2026-08-26) | `zai-org/GLM-5.3-Flash` (2026-08-27) |
| Size | 176B total (incl. a 51B N-gram table), **6B active** | 320B total, **18B active** |
| Context | 262K native (1M with YaRN, opt-in; a full 262K request is "not tested" in vLLM's recipe) | 1,048,576 declared; some hosts cap lower |
| Thinking | On by default; `enable_thinking=false` turns it off | **Always on**; the template opens `<think>` unconditionally |
| Effort | `reasoning_effort` ∈ `xhigh` (default) / `medium` / `low`; anything else is a template exception → HTTP 400 | `low` / `high` / `max`; anything else silently becomes `max` (the default) |
| Tool-call format / server parser | Qwen3-Coder XML / `qwen3_coder` | GLM XML / `glm47` |
| Licence | Reported as "Qwen Community License 1.0", with conflicting reports of a clause aimed at coding-assistant businesses — **legal must read it** | Reported MIT — not verified |

The mapping is a name match, not a confirmation: the gateway may serve a hosted
API or a quantised checkpoint, and several known bugs depend on which. "Flash" is
a price tier; neither is a small model.

### 4.2 What this means for the harness — the certain part

These come from the templates and from LiteLLM's code; they apply whichever model
wins.

- **Send reasoning back on every assistant turn, as `reasoning_content`.** Both
  templates keep *all* earlier reasoning by default (Qwen3.8 `preserve_thinking`,
  GLM-5.3 `clear_thinking=false`) and insert an empty think block where it is
  missing — so today the model sees its own past turns as thoughtless, which is not
  what it was trained on. LiteLLM passes `reasoning_content` through in both
  directions for the `openai/` and `hosted_vllm/` providers.
- **Never a system message after the first.** The Qwen template raises on it (→
  400). Nudges and checkpoints go in as user messages.
- **`content: ""`, never `null`, on tool-call turns.** `null` rendered as "None" on
  GLM-5.3-Flash and drove degenerate loops after 8-20 tool turns (fixed in vLLM; we
  do not know the gateway's version).
- **Validate tool arguments before they re-enter history.** vLLM silently replaces
  malformed arguments with `{}`, corrupting the transcript with no error.
- **Effort from a per-model allow-list; never LiteLLM's `thinking` parameter for
  Qwen** (it maps to values the template rejects). Cost is not a constraint, so
  both run at maximum: `xhigh` and `max`.
- **Vendor sampling, not 0.1.** Qwen thinking: temperature 1.0, top-p 0.95, top-k 20.
  GLM: 1.0 / 0.95. Extras travel in `extra_body`, which LiteLLM forwards.
- **Degeneration is a retryable failure.** `finish_reason=length` with empty content,
  runs of `!!!!` in reasoning, a turn that announces a call and makes none: retry
  with fresh sampling, then hand that run to the other model. Both are reported in
  long agentic sessions (GLM: SGLang #36669, opencode #45533; Qwen: malformed tool
  names deep into long sessions).
- **Code that looks like a tool call.** An open vLLM bug (#57541) parses a
  `<tool_call>` inside fenced code as a real call. A code reviewer quotes code all
  day; R0 probes it.

### 4.3 Choosing between them — a hypothesis and the test that decides

What the specifications suggest, to be confirmed or overturned:

- **GLM-5.3-Flash as the investigator**: three times the active parameters, the
  longer window, maximum effort by default. Investigation is where depth per step
  matters most.
- **Qwen3.8-Flash-Next as the second voice**: finders (many in parallel, where its
  speed helps) and the second verifier of every finding. Verifying with the *other*
  model is the point — same-model errors correlate, and there is no stronger model
  to check with.

Decided on evidence, in two steps:

1. **R0, the gateway probe** (§10): per model, the real context and output limits;
   that reasoning comes back, and is accepted when echoed; parallel tool calls; a
   30-turn soak at ~150K and ~250K tokens counting malformed calls and degenerate
   turns; the fenced-code probe; throughput and concurrency. A model that cannot
   hold the soak is not an investigator, whatever its benchmarks say.
2. **R1, the scenarios**: each role on each model, three runs per scenario. The
   choice is whichever configuration finds scenarios 1-2 with correct chains and
   keeps 3-4 silent most reliably.

If one model fails R0 outright, the other does every role, and verification gets
its independence from fresh context and a different prompt instead.

---

## 5. The engine: build or embed

### 5.1 The options

- **A. Our orchestrator, our own agent loop.** The workflow of §6.6 in code; each
  role is a run of a small loop we own.
- **B. Our orchestrator, a borrowed loop library.** Each role runs on an embeddable
  library — realistically the OpenHands SDK, the only candidate that is an
  in-process library rather than a CLI.
- **C. Our orchestrator, a headless CLI agent per role.** Each role spawns e.g. Qwen
  Code headless in the checkout, with our tools over MCP and a schema'd terminal
  output; we parse its event stream.
- **D. One headless agent does the whole review.** "Review this MR and post
  comments" to a general agent, a thin GitLab layer around it — the literal reading
  of "Claude Code on the repo".

**D is rejected first.** The review pipelines that work in practice keep the stages
in code, not in prose: claude-code-security-review and security-guidance do; Qwen
Code moved its gates into a CLI after measuring that prose defences "went
unperformed" — its review skill records approving a pull request *no agent had
read* (survey, `opencode-qwencode.md`; `SKILL.md` Step 3D). A flash model fans
out, verifies and reconciles from instructions worse than a frontier one, and a
single long session is where flash models are weakest. D also gives up the
mechanical checks that are our main defence against noise.

So the question is the inner loop: A, B or C.

### 5.2 The survey

Every project was cloned and its code read, not its README. Full notes:
[`reviewer-v2/survey/`](reviewer-v2/survey/README.md).

| Project | Licence | Offline: what must be switched off | Talks to LiteLLM / OpenAI-compatible | Reasoning across tool turns | Headless, structured output | Code navigation | Sandbox | Activity (90 days) | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **Qwen Code** | Apache-2.0 | Usage statistics **on by default** → `QWEN_USAGE_STATISTICS_ENABLED=false`; models.dev refresh off; web tools excluded | Yes, `--openai-base-url`; Qwen/GLM paths are name/host-gated, so set flags via `extra_body` | Echoed correctly | `qwen -p`, Claude-Code-compatible `stream-json`, `--json-schema` terminator, budgets with exit codes | ripgrep, LSP tool (experimental, no downloads) | bwrap/landlock per command, network closed | 3,981 commits | **Challenger C in R1**; best source of review-process ideas |
| **OpenHands SDK** | MIT | `LITELLM_LOCAL_MODEL_COST_MAP=True`; the rest opt-in | LiteLLM library, native tools; verified-models list names `glm-5.3-flash`, `qwen3.8-flash` | Stored, but echoed only with `send_reasoning_content`, **off for Qwen/GLM** unless overridden | In-process Python, typed JSON events, schema'd finish | Shell, editor, grep (paths only); no LSP | None (Docker workspace optional) | 899 commits | **Challenger B in R1**; loop-recovery ideas |
| **opencode** | MIT | `OPENCODE_DISABLE_MODELS_FETCH`, `_AUTOUPDATE`; `rg` must be pre-installed or it downloads it | Yes (`@ai-sdk/openai-compatible`) | Echoed correctly | `run --format json`, `serve` + SSE | No LSP since the July 2026 v2 rewrite | None | 5,445 commits | No: no LSP, extreme churn |
| **Codex CLI** | Apache-2.0 | Analytics, OTel metrics, plugin sync off by config | **Responses API only** — Chat Completions removed 2026-02 | n/a for us | `exec --json`, `--output-schema` (server-enforced) | Shell only | bwrap + seccomp, read-only | 4,271 commits | No: depends on an unverified LiteLLM Responses bridge; take its review rubric |
| **Goose** | Apache-2.0 | `GOOSE_TELEMETRY_OFF=1`; models.dev fetch compiled in → custom build | Yes, LiteLLM provider | Echoed correctly | `run --recipe --output-format stream-json`, `final_output` validated locally | Shell, tree-sitter call graph | None | 588 commits | Viable, but nothing B or C lacks; take its recovery and compaction ideas |
| **Hermes Agent** | MIT | Many per-feature switches; models.dev has no off switch | Yes | The best handling read: echoed by default, rejected fields learned per route | `-z` prints final text only | Shell, LSP lazy-installs | Docker backend with `--network=none` | ~50k commits | No: unauditable size and churn; take reasoning replay and memory discipline |
| **mini-SWE-agent** | MIT | `LITELLM_LOCAL_MODEL_COST_MAP`, `MSWEA_COST_TRACKING` | LiteLLM, native tools | Echoed | Trajectory JSON every step | Bash only | Docker/bwrap, network not isolated | low | No; take its loop skeleton and trajectory format |
| **SWE-agent** | MIT | Prebuilt runtime image; it pip-installs tools at startup | LiteLLM | **Dropped**; one call per turn | Trajectories, action replay | Windowed viewer, bounded search | SWE-ReX | maintenance mode | No; take bounded search and pruning |
| **Aider** | Apache-2.0 | Analytics, update check, URL scraping, model-metadata fetch | LiteLLM | — | Plain text; not an agent | **Repo map** (tree-sitter + PageRank) | None | dormant | No; take the repo map |
| **PR-Agent** | MIT (since 2026-07) | `LITELLM_LOCAL_MODEL_COST_MAP` | LiteLLM | — | Single shot, no tool loop | None | n/a | 1,505 commits | No; take its GitLab layer knowledge |
| Claude Code code-review plugin, claude-code-security-review, security-guidance | Apache-2.0 / MIT | — | — | — | — | — | — | — | Ideas only, as the brief asked: process, verification, exclusions |

### 5.3 Recommendation: A, with B and C as measured challengers

**Build the orchestrator, the tools, the memory and the traces ourselves — that is
the product. Write our own agent loop too, small, and take its hard parts from code
that has already met these models.** In R1, run the same roles, prompts and tools on
Qwen Code headless (C) and on the OpenHands SDK loop (B), and keep whichever
performs best on the scenarios. The loop sits behind one interface
(`Engine.run(role, task, tools, budget) → Conclusion`), with our tools also exposed
as an MCP server, so a swap is days, not a rewrite.

Why own the loop:

- **It is the small part.** A review loop is read-only, has no editor and no
  persistent terminal, and ends in one schema'd tool call — a few hundred lines.
  What makes it reliable on flash models is a short list of specific behaviours —
  reasoning replay, tool-argument repair, pair-safe compaction, loop detection,
  nudges on reasoning-only or empty turns — and those exist, readable, under MIT or
  Apache, with tests naming GLM-5.3 and Qwen-Flash quirks. We take the behaviours
  with attribution; we do not need the frameworks.
- **The message format is where these two models are fragile, and we must own it
  exactly** (§4.2). In a borrowed loop it is a flag we hope is right — the OpenHands
  SDK echoes reasoning for Qwen and GLM only when overridden; Qwen Code gates its
  Qwen and GLM handling on model names and hostnames, which our LiteLLM aliases may
  not match.
- **Air-gap economics.** Every upgrade is a transfer plus an egress audit. A
  dependency shipping tens of commits a day gets pinned on day one and stays pinned,
  so we carry its weight and get little of its momentum. Qwen Code additionally
  ships usage statistics on by default: one missing environment variable and it
  tries to phone home.
- **Prompts and tools shaped for review.** General agents carry system prompts and
  tool sets for "edit until the tests pass" (and OpenHands hard-codes
  `add_security_risk_prediction=True`, which adds a `security_risk` field to every
  tool that is not read-only, `sdk/agent/agent.py:955`). Each is
  workable; each is friction a flash model pays for in tokens and attention.

Why B and C still run in R1: a mature harness a model was tuned against (Qwen Code
for a Qwen model) may simply investigate better than a new loop. If it does on the
same scenarios, that is evidence our loop is missing something, the traces will
show what, and we switch rather than defend the choice.

Trade-offs accepted with A:

- We maintain a loop. Mitigated by keeping it small and by the bake-off: it is kept
  only if it is at least as good as the best borrowed one.
- We will rediscover some failure modes others have fixed. Mitigated by the steal
  list below, and by traces that show a new failure mode on its first occurrence.
- Two languages in one product (Go control plane, Python brain). Mitigated by a
  narrow JSON contract; each side is in the language that suits it, and the control
  plane is not where future work happens.

### 5.4 What we take from whom

All MIT or Apache-2.0; copied code keeps its notice, recorded in `NOTICE`.

| From | What | Lands in |
|---|---|---|
| Qwen Code review skill | Finder roster by dimension, each owing a *receipt* of what it walked; a coverage gate computed from transcripts, not self-report; anchors resolved from a quoted snippet, not a line number; removed-behaviour audit (the `-` lines); producer-direction tracing (who reads a field the diff adds); the recall rule (finders never drop a suspected critical); one verifier per small shard; the witness rule (prefer executed evidence); iterative reverse audit until two dry rounds; root-cause aggregation | §6.6 |
| security-guidance (Anthropic) | Investigate → second "find something different" pass → in-diff/off-diff tagging → adversarial refute-with-citations; an off-diff finding must name the changed line that enables it; repo guidance is additive-only | §6.6, §6.8 |
| Claude Code code-review plugin | Fresh-context validator per finding; the exclusion list (pre-existing, intended, linter territory, nitpicks); one comment per issue; a suggestion only if it fixes the issue completely; draft privately, then post; links with the full SHA | §6.6 |
| Codex | Review rubric: introduced by this change, provably affects other code, not speculative; stop hooks that refuse to finish before verification | §6.5, §6.6 |
| OpenHands SDK | Response classification (tool calls / content / reasoning-only / empty) with targeted nudges; pair-safe condensation; stuck-detector thresholds; GLM JSON-string argument decoding; Qwen raw-newline argument repair | §6.5 |
| Hermes Agent | Reasoning replay policy, including learning per route which fields the gateway rejects; memory as a bounded snapshot with grounding rules and a do-not-capture list; "Unverified / working hypotheses" as a compaction section; a stop gate on unverified claims | §6.5, §6.8 |
| Goose | Thinking-only recovery; spill large tool output to a file with a preview; structured JSON compaction summary | §6.5 |
| Aider | The repo map: `*-tags.scm` queries, personalised PageRank over the def→ref graph, `TreeContext` rendering. Pin `tree-sitter-language-pack` 0.13.0 — 1.x downloads grammars at first use | §6.4 |
| SWE-agent | Search that refuses over-broad queries and says how to narrow them; pruning of old observations | §6.4, §6.5 |
| mini-SWE-agent | Loop skeleton with exception-driven exits; trajectory saved every step with raw responses | §6.5, §6.9 |
| PR-Agent | GitLab: position rules for added/removed/context lines, a SHA-match guard before posting, a file-level thread fallback when a line is rejected, "may have landed" classification for idempotent writes, resolving only threads whose line was actually removed | control plane |

### 5.5 Built, integrated, thrown away

| | What |
|---|---|
| **Built** | The brain: orchestrator, agent runtime, review roles, thread agent, cartographer; tier-1 code intelligence; trace store, viewer, replay, probe; multi-repo scenario fixtures and their scorer. In the control plane: durable queue, workspace builder, dependency materialisation endpoint, finding store with evidence chains, per-thread single-flight. |
| **Integrated** (vendored, offline) | ripgrep; tree-sitter with `tree-sitter-language-pack` 0.13.0; Aider's repo-map code and grep-ast (adapted); networkx; language servers for the languages we review (e.g. gopls, basedpyright); bubblewrap; pydantic and httpx (we call the LiteLLM *proxy* over HTTP, so the LiteLLM library and its cost-map fetch are not needed). For R1 only: Qwen Code and the OpenHands SDK as challengers. |
| **Kept** | Go: webhook and signature check, worker pool, GitLab client, git transport with the go-git/Basic-auth fallback, stores, cron scaffolding, CLI. Python: the `eval/` harness and its 13-case corpus, as a cheap regression check. |
| **Thrown away** | Everything in `agent/` that thinks: the structured two-pass review, the unused reviewer agent, replier and verdict single calls, the `prompt/` and `tools/` packages, risk scoring, chunking, the planner, keyword intent classification, the fingerprint-and-phrase filters, the feedback consolidator. Not because they are badly written — most are careful — but because each encodes the small-model, short-context assumption this redesign removes. |

---

## 6. Architecture

### 6.1 Shape

```
                      ┌──────────────────── control plane (Go, kept) ────────────────────┐
 GitLab ──webhook──▶  │ webhook ─▶ durable queue ─▶ worker ─▶ workspace builder           │
   ▲                  │                                   │      (mirrors, worktrees,     │
   │                  │                                   │       dependency repos)       │
   │   threads,       │                                   ▼                               │
   └── replies ◀──────│ poster ◀── result.json ◀── brain subprocess (one per job) ────────│──▶ LiteLLM
                      │   ▲                         │  trace dir                         │    (the only other
                      │   └ finding store (pg)      ▼                                    │     egress)
                      │                         trace store ─▶ trace viewer / replay      │
                      └───────────────────────────────────────────────────────────────────┘
                                                   │
                       brain (Python, new) ────────┤ orchestrator: the review workflow, in code
                                                   │ agent runtime: our loop, one run per role
                                                   │ code intelligence: tree-sitter, ripgrep, LSP
                                                   │ sandbox runner: bwrap, no network
                                                   └ repo memory: markdown in a git repo
```

The split is "who fetches the bytes" versus "who thinks about them":

- **The control plane stays in Go and shrinks.** It holds the hard-won VPN
  knowledge and it works: webhook validation and dedup, worker pool, per-project
  locks, the GitLab client, the git transport with the go-git fallback
  (`internal/pkg/git/manager.go`), three store backends. Nothing in the brief is a
  complaint about it. It gains a durable Postgres queue (replacing the in-memory
  one), a workspace builder, a finding store that keeps evidence chains, and
  per-thread single-flight.
- **The brain is a new Python program**, one subprocess per job, with a JSON
  contract. It never talks to GitLab: everything it needs is in the job file, and
  it asks the control plane to fetch dependency repositories. Why Python, when the
  last decision was Go: that decision (ROADMAP, *Key decisions*) rested on
  "editing review behaviour is editing text". It no longer is — the brain *is* the
  product, most future work happens in it, and the team writes Python. It also
  lines up with the code we borrow (Aider, mini-SWE-agent, OpenHands, Hermes are
  Python), with prebuilt tree-sitter wheels, and with `eval/`, which can then drive
  the brain directly on a local repo with no GitLab — fixing a real defect today:
  `cmd/cli review` cannot run without a live GitLab (ROADMAP, *Gotchas*).

The process boundary is also a trust boundary: the brain has the gateway key and
read-only access to the workspace; anything that executes repository code runs one
level further in, without network or secrets (§6.11).

### 6.2 The job contract

```text
habodek-brain review  --job job.json --out result.json --trace DIR
habodek-brain thread  --job job.json --out result.json --trace DIR   # a reply or a re-check
habodek-brain map     --job job.json --out memory.patch --trace DIR  # memory upkeep
habodek-brain replay  DIR [--model M] [--from-stage S] [--cassette]
habodek-brain probe   DIR --hypothesis "…"
```

`job.json` carries the MR (title, description, linked issue, author, labels,
branches), base/head/start SHAs, the workspace layout (head, base and dependency
worktrees and their versions), the open threads with their stored findings, the
memory path and commit, the posting policy and the model routing. `result.json`
carries findings with evidence chains, the summary text, per-stage statistics and
the run id. The control plane posts; the brain decides.

### 6.3 Workspace: the right code at the right version

Per job, under `jobs/<id>/`:

- `head/` — `git worktree add --detach` from the project mirror at the MR head. A
  real working tree, because ripgrep, tree-sitter, language servers and test
  runners want files, not `git show`.
- `base/` — the merge base, created lazily.
- `deps/<name>@<version>/` — other repositories the change reaches, at the version
  this commit actually uses:
  1. The manifest and lockfile (`go.mod`, `pyproject.toml` + lock,
     `package-lock.json`, `pom.xml`, …) give package → version.
  2. A per-organisation map in repo memory gives package → GitLab project and tag
     convention (`v{version}`). For packages from an internal registry, the source
     artefact itself (sdist, module zip) is the fallback.
  3. The brain asks the control plane to materialise `project@ref`; it fetches into
     its mirror with the existing transport and adds a worktree. Lazily: an
     investigator that follows a call into a library triggers it mid-run, so we
     never fetch the world up front.
- **Reverse dependencies.** For an MR *in a library*, the blast radius is in other
  repositories. The cartographer (§6.8) keeps a consumer index — which projects
  depend on which internal package, at which version — and `references` can search
  consumers' default branches. Without it, a library change can only be judged by
  its own tests.

Mirrors stay partial (`--filter=blob:none`) and shared; worktrees are disposable;
indexes are cached by blob SHA, so the next review of a repo re-parses only what
changed.

### 6.4 Code intelligence

Exploration is what the brief says is missing, so it gets the most deliberate
design: two tiers behind one interface, and every result says which tier answered.

**Tier 1 — every language, no configuration.** ripgrep for text, tree-sitter for
structure. From tag queries (the `*-tags.scm` approach of Aider's repo map) we build,
per file and cached by blob SHA, definitions and references with their enclosing
definition:

- `definition(name)`, `references(name)` — each hit classified (call, import, type
  use, other) with its enclosing function and package. Lexical, so it over-matches
  common names; classification and grouping keep it readable rather than hiding
  the noise.
- `callers(fn, depth)` — the transitive caller tree.
- `outline(path)`, and a ranked repo map for orientation (personalised PageRank,
  seeded with the changed files and the symbols in the diff).
- `changed_symbols` — the **change map**: per changed file, definitions added,
  removed or modified; signatures changed; exported names, constants, enum members
  and struct fields changed — computed by comparing base and head syntax trees, not
  by reading the diff.

**Tier 2 — precise, per language, where an offline server exists.** gopls,
basedpyright, typescript-language-server, jdtls, rust-analyzer, started per job
against the workspace with `deps/` on the resolution path. They answer what tier 1
cannot: references to *this* method rather than every method of that name,
implementations of an interface, the type of an expression. Which to ship depends
on the languages we review (§12).

**Git.** `git_log(path|symbol)`, `git_blame(path, lines)`, `git_show(sha)`,
`diff(from, to, path)`. "This line was changed eight months ago in *fix double
charge on retry*" is context a human reviewer uses and today's bot cannot see.

**Execution.** `run(cmd)` in the sandbox (§6.11): build, type-check, lint, or a
targeted test, on head and when useful on base. A compile error in a consumer two
packages away is a finding no one argues with. Opt-in per repository, because it
needs a recipe (how to build this repo offline) and the toolchain in the image.

**Reading without the old limits.** `read(path)` returns the whole file with line
numbers up to 2,000 lines / 100 KB, then pages. `grep` takes full regex and reports
totals when it truncates ("312 matches in 40 files, showing 60 — narrow with
`path_glob`"). Nothing is truncated silently, and nothing from the top.

### 6.5 The agent runtime

Our own loop, small on purpose. Its job is to make a flash model's long session
reliable:

- **Native tool calls only**, OpenAI-compatible, through LiteLLM. Every structured
  output is a tool call with a JSON schema — `propose_hypotheses`, `conclude`,
  `verdict`, `write_comment` — never JSON scraped from prose. Invalid arguments come
  back as a tool error naming the violation; three failures end the run as
  `malformed`, recorded, never guessed at.
- **Reasoning echoed per model** (§4.2), thinking at maximum effort, vendor
  sampling, a large per-turn output budget.
- **One role, one bounded question, fresh context.** A run gets a narrow objective,
  a tool budget (default 40 calls for an investigator, 20 for a verifier), a
  wall-clock limit, and must end with its terminal tool. Flash models are much
  better at "settle this one question" than at "review this MR".
- **Context management in two steps.** Above ~60% of the window, old tool *outputs*
  are elided (`[elided: read src/x.py 1-420 — re-read if needed]`), keeping the
  model's own reasoning and notes. Above ~80%, older turns are summarised by the
  model into a fixed structure — established facts with `file:line`, working
  hypotheses (marked unverified), open questions, files already read. The task and
  the evidence so far are pinned and never compacted. An assistant `tool_calls` turn
  and its `tool` results are kept or dropped together — today's defect.
- **Loop guards.** A repeated identical call returns "already done, same result".
  Five identical calls, or N turns with no new file read or note, trigger a
  checkpoint ("state what you know and what you will do next"). Reasoning-only or
  empty turns get a targeted nudge. Budget exhaustion forces `conclude` with what
  was established.
- **Everything is traced** (§6.9).

### 6.6 The review workflow

Stages in code, not in a prompt (§5.1).

| Stage | What runs | Output |
|---|---|---|
| 0. Gate | code | Skip drafts, bot MRs, generated/lock/vendor-only changes; record why. |
| 1. Change map | code (§6.4) | Changed symbols, mechanically visible contract changes, their references across repo and dependencies. |
| 2. Understand | lead agent | What the MR is for — title, description, linked issue, the diff, changed files read in full, repo memory — and the contracts it changes, explicit and implicit ("now returns `None` instead of raising"). |
| 3. Hypothesise | ~10 finder agents in parallel, both models | Each owns a dimension: intent fidelity (does it do what it claims?); line-by-line correctness in the enclosing function; removed behaviour (what did each `-` line enforce, and where is that re-established?); cross-file tracing in both directions (every caller of a changed export against the new contract; every reader of a field the diff adds); failure handling, resources, concurrency; security; wrappers, caches and adapters; history and rules (`git log`/`blame` on touched lines, earlier threads, `.habodek.md`); and three personas — attacker, 3 a.m. on-call, the maintainer six months on. They return **hypotheses, not findings**: claim, why suspected, where to look, what would refute it. Each owes a receipt of what it walked ("traced 7 changed exports to 23 call sites"); a bare "nothing found" is treated as not having run, relaunched once, then recorded as an unreviewed dimension. A suspected critical is never dropped here. |
| 4. Coverage gate | code | From the transcripts, not self-report: every finder ran, opened its assigned diff and files, and returned a receipt. |
| 5. Triage | lead agent | Merge duplicates, drop what the change cannot cause, order by potential impact. |
| 6. Investigate | one agent per hypothesis, in parallel | `confirmed` / `refuted` / `inconclusive`, with an **evidence chain** of `{path, line, quote, claim}` links from the changed line to the failure; a concrete failing scenario; the changed code that causes it, quoted; a fix. Where a run or test can settle it, it runs it — a *witness*. New questions go back to stage 5. |
| 7. Mechanical checks | code | Every quote must exist at that path and revision. The anchor is resolved from the quoted snippet against the diff hunks, never from a model's line number. Qwen Code does the same — its agents got 21 of 22 line numbers right, but a miscount fails silently, and a derived line beats an asserted one. Here it also addresses the F5.1 drift, where the comment landed on the enclosing `def`: the quote names the defective line itself. A finding about unchanged code must name the changed line that enables it, or it is dropped. Same root cause → one finding listing its sites. |
| 8. Adversarial verification | two verifiers per finding, one per model, fresh context | Each gets the claim and chain — not the investigator's transcript — and is told to refute it with cited evidence: a guard upstream, an unreachable path, a test proving otherwise, a misread contract, pre-existing behaviour, intended by the MR. `upheld` / `refuted` / `uncertain`, with evidence. |
| 9. Reverse audit | finder agents, iterated | Given everything found so far: what is *not* on the list? New hypotheses go through stages 6-8. Stops after two consecutive dry rounds, or a round cap. |
| 10. Decide and write | judge agent + code | Post only what both verifiers upheld. Rank, apply the comment budget, write each comment, write the summary, re-read the planned set as a whole ("would a senior engineer want each of these?"), check that the MR head has not moved. |

**Failure is closed.** A verifier that errors or times out counts as `refuted` for
posting and is recorded as such. The surveyed Anthropic filters fail open; with a
weaker model and trust as the scarcest asset, we do the opposite.

**A comment:**

> 🟠 **`get_user()` now returns `None` for a missing user, and two callers dereference it.**
>
> `store/users.py:41` returns `None` where it used to raise `NotFound`.
> - `api/profile.py:31` — `user = store.get_user(uid)` then `user.email` → `AttributeError`, served as a 500 where the route returned 404.
> - `jobs/digest.py:12` — same pattern; the nightly digest dies on the first deleted account.
> - `cache/users.py:58` negative-caches only on `NotFound`, so misses are no longer cached either.
>
> **Fix:** keep raising `NotFound` here, or update the three call sites above.
>
> <sub>Checked 14 references to `get_user` in 3 packages · upheld by two independent verifiers · trace `r-2026…`</sub>

Each `path:line` links to the blob at the head SHA. The anchor is where the fix
belongs; consequences are cited in the body — what GitLab's inline model allows and
today's `invalid_line` drop makes impossible.

### 6.7 The second half: one thread agent

Replies and post-push re-checks become one agent with one procedure, because they
ask the same question: *does my finding still hold at HEAD, given what the developer
did and said?*

**It starts from** the stored finding — claim, full evidence chain, failing
scenario, the head SHA it was made at — plus the whole thread, plus
`diff(finding_sha, HEAD)`. Every line in the chain is mapped forward through that
diff, so it reads the code where it now is.

**It runs when:**
- a human writes in a bot thread (debounced a minute, so "fixed" and a push arrive
  as one event);
- a push touches *any file in the finding's evidence chain* — not only the anchor
  file, so a guard added in the caller triggers it;
- single-flight per thread: a newer event supersedes a queued one, and a reply and
  a re-check never both answer.

**It does:**
1. Read the developer's claim for what it is — fixed; handled elsewhere (where?);
   by design; not reachable; a question; a disagreement — with the model, not
   keywords.
2. Re-walk the original chain at HEAD, link by link.
3. Go where the developer points. "Handled in the middleware" means opening it and
   establishing that it is on *every* path from the entry points in the original
   blast radius to the failure, not that it exists.
4. For "fixed": check every site in the chain and every sibling the blast radius
   found, not only the anchored line, and that the fix does not break the same path
   differently.
5. Conclude: `fixed` · `fix_incomplete` (naming what remains, `path:line`) ·
   `stands` (the scenario that still fails) · `withdrawn` (conceding plainly, and
   saying what convinced it) · `need_info` (one precise question).
6. Have the conclusion attacked by the other model: `withdrawn` must survive "is the
   developer right, or just confident?"; `fixed` must survive "is this a fix, or a
   change near the line?". On disagreement, one more round of investigation, then
   `unclear` with both arguments — never a coin toss.

The bot still never resolves threads; the developer does, having read a real
conclusion. Following the managed Claude Code review's rules: the summary of a
re-review never says "LGTM" while blocking threads are open, says how many remain,
and never re-posts a finding a person resolved because a push moved its lines.

### 6.8 Repository memory

So that each review does not start from zero. Three layers, all markdown, in a git
repository of their own (`habodek-memory`, one directory per project) — readable,
diffable, correctable by a person, carried across the air gap like any repo:

1. **The repo card**, written and maintained by a *cartographer* run: modules and
   responsibilities, entry points, layering and the rules it implies, public APIs
   and their consumers (the reverse-dependency index), cross-repo dependencies and
   how to resolve them, invariants worth knowing ("every handler under `/admin` gets
   `require_admin` from the router"), hot spots from history, the offline
   build/test recipe. The first build is a long run (an hour is fine), then
   incremental on default-branch merges that change structure.
2. **`.habodek.md` in the repository**, optional, owned by the team, read from the
   *target* branch (an MR cannot edit the rules it is judged by), scoped by
   directory, and additive only: it can add checks and context, never suppress a
   class of finding. That is a security property — MR content is untrusted.
3. **Lessons** from *verified* thread outcomes only: a finding withdrawn because the
   developer showed a guard the bot missed becomes "in this repo, X is guaranteed by
   Y (see …)". Each lesson cites its threads and carries a count; low-support lessons
   are not injected; one-off replies and environment failures are never captured.
   This replaces the consolidator that rewrites a prompt from feedback one-liners.

The card and the strongest lessons are always in context (kept short); the rest is
reachable with `recall(query)` (BM25 over the files — no embedding model needed
offline). Each run records the memory commit it used, so replays are exact.

### 6.9 Observability and replay

Without this, "why did it miss that?" has no answer, so it is part of the product.

- **A trace directory per run**: `run.json` (inputs: job file, SHAs, model ids,
  prompt versions, memory commit, policy); `events.jsonl` (every LLM request and
  response including reasoning, every tool call with its full result, every stage
  transition, timings, tokens); `findings.json` (every hypothesis and finding
  through its life — proposed, investigated, verified or refuted, posted or dropped —
  *with the reason and the stage that dropped it*); and a readable `report.md`.
- **`habodek trace show RUN`** renders a single static HTML file: the hypothesis
  tree, each investigation's tool calls, the verifiers' arguments. Offline, no
  server.
- **Replay** from recorded inputs (workspace rebuilt from SHAs): against recorded
  LLM responses to debug the harness deterministically, or from a given stage with
  another model or prompt to compare.
- **Probe**: `habodek-brain probe RUN --hypothesis "…"` runs one investigator on a
  stated hypothesis in that run's workspace. It splits a miss into its two causes:
  nobody thought of it (stage 3), or it was investigated badly (stage 6).
- **Metrics** per run and repository from the same records: hypotheses raised,
  investigated, confirmed, refuted, posted; thread outcomes; acceptance by category.

### 6.10 Noise

A stronger reviewer talks more. Every stage pushes the other way:

- Finders produce hypotheses, not comments; most die in investigation.
- Posting needs a mechanically valid evidence chain and two independent `upheld`
  verdicts. Inconclusive is silence.
- A comment budget per MR (default 5; criticals exempt) and a minimum severity
  (default medium), per repository.
- One comment per root cause. No style, naming or "consider" comments unless
  `.habodek.md` asks for them. A suggestion block only when it fixes the issue
  completely.
- Existing threads are matched semantically before anything is posted.
- Acceptance rate per repository and category, from thread outcomes; a category
  that falls below a threshold has its bar raised automatically, logged.
- Silence is a first-class outcome. On a clean MR the summary is one line: what was
  checked, and that nothing survived verification.

The headline metric stays the one this project chose: zero comments on clean MRs,
then precision, then detection.

### 6.11 Sandbox

- **The brain**: read-only access to `jobs/<id>/`; egress only to the LiteLLM base
  URL and the control plane's internal endpoint.
- **`run(cmd)`** one level further in: bubblewrap (or a rootless container if the
  host has one) with no network namespace, a read-only root, a throwaway writable
  copy of the worktree, a cleared environment — the gateway key is never visible to
  repository code — and CPU, memory and time limits.
- **Repository text is data.** Prompts say so; output goes only through schema'd
  tools; `.habodek.md` cannot suppress findings. The worst a prompt injection in an
  MR can do is push a wrong comment onto that same MR — which the evidence checks and
  two verifiers exist to stop.

---

## 7. Getting it onto the VPN and running offline

The rule — everything open source, vendorable, nothing calling out at runtime —
shapes the build, not only the deployment.

| Artefact | Contents |
|---|---|
| `habodek-control` | The Go binary. Builds with `GOFLAGS=-mod=vendor GOPROXY=off`, from a fresh clone (after the `.gitignore` fix). |
| `habodek-brain` image or wheelhouse | Python 3.12 and the brain's wheels; ripgrep; git; bubblewrap; tree-sitter grammars compiled into wheels; the chosen language servers (gopls is a static binary; Python/TS servers need a bundled Node runtime). |
| `habodek-toolchains` (optional, per language) | Toolchains for `run(cmd)`: Go, Python, Node, JDK — whichever we review. |
| `habodek-memory` | A git bundle; empty on first install. |

With Docker or Podman on the VPN host, these ship as images (`docker save | zstd`,
checksums, an SBOM and a licence report, all generated off-VPN). Without, as a
wheelhouse plus static binaries, the way `HANDOFF.md` moves Python today (§12).

**"No outbound calls" as a tested property:**

- Our code talks to exactly two configured hosts — the LiteLLM base URL and GitLab
  — and only the control plane talks to GitLab.
- Known third-party defaults are pinned off in the image environment, each with the
  reason: `LITELLM_LOCAL_MODEL_COST_MAP=True` wherever a LiteLLM library is present,
  `DO_NOT_TRACK=1`, `HF_HUB_OFFLINE=1`, tokenizer caches pointed at bundled files,
  language-server auto-download disabled, and for the R1 challengers,
  `QWEN_USAGE_STATISTICS_ENABLED=false` and `QWEN_CODE_MODELS_DEV_REFRESH=off`.
- **The egress test**, before every transfer: the whole scenario suite runs in a
  network namespace where only a stub gateway is reachable; every other connection
  attempt is logged and counted. Anything above zero fails the build. This catches a
  dependency that starts phoning home after an upgrade.
- `tree-sitter-language-pack` stays on 0.13.0 (1.x downloads grammars at runtime).

---

## 8. How long a review takes

> **Superseded by the 25 RPM answer** — see the handoff §3.1. At ~25 requests per
> minute the request count, not latency, sets the time: a typical MR is budgeted at
> ~250 requests, which comes to **15-30 minutes** with an empty queue. A reply takes
> **2-5 minutes** and is prioritised. The whole service manages **about 4-6 typical
> reviews an hour**. The estimate below assumed ample concurrency and is kept only
> for the record.

Estimated, not measured; the gateway's throughput and concurrency decide it, and R0
measures both. Assumptions: a thinking turn of 1-3k output tokens at 60-150
tokens/s; prefill up to ~100k tokens mostly served from prefix cache after the first
turn; so 10-25 s per agent turn; about 20 concurrent requests per review.

| Stage | Turns per agent | Wall clock |
|---|---|---|
| Workspace + change map (warm mirror and index) | — | 0.5-2 min |
| Understand + hypothesise (finders in parallel) | 15-30 | 4-8 min |
| Investigate (~10-15 hypotheses in parallel) | 20-40 | 6-12 min |
| Verify (two per finding) and reverse audit, overlapping | 10-20 | 5-10 min |
| Decide and write | 5-10 | 1-2 min |

- **Typical MR** (≈10 files, a few hundred changed lines): **20-35 minutes**.
- **Large MR** (50+ files): 40-75 minutes; finders split by area, not file count, so
  cross-file reasoning survives.
- **A reply or re-check: 3-8 minutes.** This one is felt — someone is waiting in the
  thread — so it is prioritised in the queue.
- **First review of a new repository** adds the cartographer run (up to an hour,
  once) and first-use dependency fetches.
- With fewer concurrent requests, reviews get slower, not worse.

A one-line "review in progress" note goes up when a review starts, so a long silence
is not mistaken for a clean result.

---

## 9. Demo scenarios

Nine scenarios as small, realistic multi-repository fixtures, run end to end: the
brain CLI on local repos first, then through GitLab on the local rig. Each is also
reviewed by Claude Code for a side-by-side read — not a score; whether a person
would rather have Habodek's comments. The fixtures are synthetic, so running Claude
Code on them outside the VPN exposes nothing. Sketched in Python here; built in the
languages we actually review (§12).

The fixture world: `payments-lib` (a library repo, versioned by tag),
`platform-common` (shared auth, storage, HTTP error mapping) and `orders-service`
(the consumer) — each its own git repository.

**First half**

| # | Scenario | What a good review does |
|---|---|---|
| 1 | **A bug across three libraries.** `platform-common/store.get_user()` now returns `None` for a missing user instead of raising `NotFound`; its tests are updated and the diff looks tidy. The contract lives in `platform-common/cache` (it negative-caches only on `NotFound`); the broken consumer is `orders-service/api/profile.py`, which dereferences the result. | Finds the changed contract, walks references into the other repository, comments on the changed line with the chain to both consequences. |
| 2 | **A clean diff that breaks something far away.** `orders-service` bumps `payments-lib` 1.4.2 → 1.5.0: one line in `pyproject.toml`. Between those tags `charge()` stopped raising `PaymentDeclined` and returns a result object; `domain/checkout.py` still relies on the exception to cancel the order, so declined payments now ship. | Treats a dependency bump as a change to review: diffs the library between the two tags, finds the behaviour change, finds the consumer relying on it. |
| 3 | **A clean MR — silence.** A refactor moves validation into a shared helper and updates all four call sites in two packages. A tempting "missing `None` check" is in fact guaranteed by every caller. | Posts nothing; the summary says what was checked. |
| 4 | **Looks wrong, isn't — silence.** A new `/admin/refunds` handler with no visible auth check; auth comes from middleware `platform-common` registers on the whole `/admin` prefix. | Finds the middleware, confirms the route is under it, posts nothing. |

**Second half** — each starts from scenario 1's finding, already posted.

| # | Scenario | Correct verdict |
|---|---|---|
| 5 | **A real fix.** `get_user()` raises again; the cache test is updated. | `fixed`, naming what changed. |
| 6 | **A partial fix.** `api/profile.py` gets a `None` check; `jobs/digest.py` still dereferences. | `fix_incomplete`, citing `jobs/digest.py:12`. |
| 7 | **"Fixed", nothing fixed.** The developer replies "fixed" and pushes an unrelated change. | `stands`, showing the unchanged path. |
| 8 | **Justified pushback.** "The `require_user` decorator turns a missing user into a 404 before the handler runs" — and it is on every route involved. | `withdrawn`, saying what convinced it; a lesson is recorded. |
| 9 | **Unjustified pushback.** The same claim, but the digest job never goes through the decorator. | `stands`, citing the uncovered path. |

The 13-case corpus in `eval/` stays as a cheap regression check — its two clean cases
still test the most important property — but it is single-file by construction and
cannot test what this redesign is for.

---

## 10. Build plan — riskiest bet first

> **Superseded by the handoff §7 (milestones M0-M12).** No gateway in development
> means the riskiest bet is measured on the VPN by the owner, compactly: an early
> probe (M1) and optional scorecards after the review (M6) and thread (M7)
> milestones. The engine bake-off is optional (handoff §3.7). The reasoning below
> still holds.

The riskiest bet is not the orchestration or the tools; those are engineering. It is
this: **that a flash model, through our gateway, can hold a 30-60-call investigation
across repositories without losing the thread, and that the other model can refute
its wrong findings well enough that clean MRs stay silent.** If that fails, nothing
else matters, so it is tested first and in isolation.

- **R0 — The gateway, day one** (half a day, needs the VPN). A probe script run
  there: `/model/info` per model (real context and output limits); thinking
  controls; whether `reasoning_content` comes back and whether echoing it is
  accepted; parallel tool calls; a 30-turn soak at ~150K and ~250K tokens per model
  counting malformed and degenerate turns; the fenced-code probe; throughput and
  concurrency. The full checklist, with what to ask the gateway's owner, is in
  survey `models.md` §8. Output: a short report that fixes the model settings and
  replaces the estimates in §4 and §8.
- **R1 — The investigation engine on scenarios 1-4.** The two repo fixes from §1
  first. Then the brain CLI against local fixture repos: agent runtime, tier-1 code
  intelligence, the lead/finder/investigator/verifier roles, the mechanical checks,
  the trace. Each scenario three times per configuration (single-run comparisons
  are noise — ROADMAP F5.1). The same roles on Qwen Code headless and on the
  OpenHands SDK, same model, prompts and tools. Claude Code on the same four for the
  side-by-side. **Gate:** scenarios 1 and 2 found with a correct chain in at least 2
  of 3 runs; 3 and 4 silent in 3 of 3; traces showing real multi-hop exploration. If
  it fails, we stop and report why before building anything else.
- **R2 — The thread agent on scenarios 5-9.** Each correct verdict in 3 of 3 runs for
  7 and 9 — the two that decide whether developers trust it — and 2 of 3 for the rest.
- **R3 — Into the control plane.** Job contract, durable queue, finding store with
  evidence chains, per-thread single-flight, posting with PR-Agent's GitLab
  lessons; delete the old brain. End to end through GitLab on the local rig.
- **R4 — Reach.** Dependency materialisation and the reverse-dependency index;
  tier-2 language servers; sandboxed `run(cmd)` with per-repo recipes.
- **R5 — Memory and calibration.** Cartographer, `.habodek.md`, lessons, acceptance
  tracking and automatic bar-raising.
- **R6 — Ship.** Offline bundle, egress test, VPN install, one friendly repository,
  then wider.

`ROADMAP.md` is updated at the end of each step with what was decided and why.

---

## 11. Risks

| Risk | Why it might bite | What we do |
|---|---|---|
| Flash models lose the thread in long investigations | The defining weakness of small-active-parameter models | Short single-question runs; pinned task and evidence; R1 measures it before anything else is built |
| The gateway mishandles reasoning across tool turns | LiteLLM sits between us and the server; behaviour depends on version and provider prefix | R0 tests it directly; a per-model policy, with "no echo" as a measured fallback |
| Verifiers agree with investigators too easily | Same-model errors correlate | The other model verifies; verifiers see claims, not transcripts; quotes checked mechanically |
| Known degeneration bugs in the serving stack | Open issues on both models in long agentic sessions | Detection and retry with fresh sampling; hand the run to the other model; report to the gateway owner with traces |
| Lexical references over-match on common names | Tier 1 is text plus syntax | Classification and grouping; tier 2 where a server exists; investigators confirm by reading |
| Reviews take 20-35 minutes | Investigation is serial within a hypothesis | Acceptable for a background reviewer; replies prioritised; an in-progress note |
| Memory learns the wrong lesson | Developers dismiss real findings | Only verified outcomes become lessons; each cites its threads; people can edit them |
| The Qwen model's licence restricts our use | Conflicting public reports | Legal reads it before R1; GLM alone covers every role if needed |
| Two languages to maintain | Go control plane, Python brain | Narrow contract; the control plane shrinks and stabilises |

---

## 12. Questions — answered 2026-11-24

| # | Question | Owner's answer |
|---|---|---|
| 1 | Gateway access from a dev session | No. The owner tests the whole feature at the end on the VPN; short outputs from a specific command can be pasted back by hand. |
| 2 | Languages | Python first; C/C++ hoped for later, low priority. |
| 3 | How internal libraries reach consumers | Installed in the venv it runs in, and available from Artifactory under the VPN. |
| 4 | The VPN host | Linux with Docker; CPU and RAM available on request. |
| 5 | Gateway capacity | About 25 requests per minute. |
| 6 | The Qwen licence | No concern. |

What each answer changes, and the questions still open, are in the handoff §2.
