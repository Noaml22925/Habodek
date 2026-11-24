# Reviewer v2 — implementation handoff

**For the session that builds it.** Written 2026-11-24. The direction in
[`reviewer-v2.md`](reviewer-v2.md) is approved; the owner asked for this file so that
a fresh session can start implementing. Read it in full before writing code. It is
self-contained for *what to build and how to know it is done*; the design doc holds
the *why*, and the survey notes in [`reviewer-v2/survey/`](reviewer-v2/survey/README.md)
hold the evidence.

**Where this file and the design doc disagree, this file wins.** The owner's answers
(§2) arrived after the design was written and changed several parts of it.

Read order: this file → `reviewer-v2.md` §0, §2, §5.3, §6 → the top of
`ROADMAP.md` → a survey note only when you are about to borrow from that project.

---

## 1. The goal

Habodek reviews GitLab merge requests in the background. The bar: its reasoning is
equivalent to opening Claude Code on the repository and asking "review this MR and
post comments" — read the code, map who calls what changed (across packages and
libraries), form hypotheses, investigate each until confirmed or refuted, verify
independently, and comment with a `file:line` evidence chain from the change to the
failure. Then, when a developer pushes a fix or answers ("fixed", "handled in X",
"by design"), check it the same way: go to the code, verify, change its mind only on
evidence. All unattended. **The product is judged by quality, not by a metric**, and
the headline property is still: *silence on a clean MR*.

## 2. Constraints — and the owner's answers of 2026-11-24

Fixed from the brief:

- Runs on a Linux machine inside a VPN with **no internet**. Every tool, library and
  agent must be open source and vendorable, and **nothing may call out at runtime**
  except the LiteLLM gateway and GitLab.
- Models come from the company LiteLLM gateway (OpenAI-compatible
  `/v1/chat/completions`): "qwen3.8 flash next" and "GLM5.3 flash" — most likely
  Qwen3.8-Flash-Next and GLM-5.3-Flash (design §4.1). **Tokens are unlimited**;
  latency matters less than quality.

The owner's answers to design §12:

| # | Question | Answer | What it changes |
|---|---|---|---|
| 1 | Gateway access from dev sessions | **No.** The owner tests the whole feature on the VPN at the end. They can run a specific thing for us and paste the output back **only if it is short** (manual copy). | No real model during development. Every command meant for the VPN prints a **≤ 20-line** summary and writes details to files there. Real-model checks are few and deliberate (§5). The R1 bake-off against Qwen Code / OpenHands becomes optional (§3.7). |
| 2 | Languages under review | **Python** first. C/C++ hoped for later, low priority. | Tier-2 code intelligence is Python-only for now (§3.4). Keep a language seam; do not build C/C++ support yet. Scenarios are Python. |
| 3 | How internal libraries reach consumers | **Installed in the venv** it runs in, and **in Artifactory** under the VPN. | Dependency source comes from site-packages and from Artifactory via `pip download` — not from GitLab repos by tag (§3.5). |
| 4 | VPN host | **Linux, Docker installed**, CPU and RAM available on request. | Ship as Docker images. The sandbox is a sibling container started by the control plane (§3.6). |
| 5 | Gateway capacity | **About 25 requests per minute** (the owner's belief). | The biggest design change: requests are scarce, tokens are free (§3.1). |
| 6 | Qwen licence | **No concern.** | Both models are usable for every role. |

Unknowns to settle with a **one-line question** to the owner when they matter
(not before):

- Is 25 RPM per key, per model, or total? (Changes the limiter's shape, §3.1.)
- What are the exact model names on the gateway? (Needed for the probe, M1.)
- Does the VPN Artifactory also proxy public PyPI and Docker images? (Decides
  between "build on the VPN from Artifactory" and "ship a wheelhouse plus saved
  images", M12.)
- Is "the venv it runs in" the bot's own venv, or the reviewed project's venv on
  the same host? (§3.5 supports both through configuration; ask when wiring it.)

## 3. Decisions in force

The design's decisions stand (summary: `reviewer-v2.md` §0). These are the ones the
answers above changed or sharpened, stated as instructions.

### 3.1 Requests are scarce, tokens are free

At ~25 RPM, a review that makes 1,000 requests takes 40 minutes of quota no matter
how parallel it is. So:

- **A global rate limiter with priorities** in front of the gateway, shared by every
  job: replies > re-checks > reviews > background (cartographer). It lives in the
  control plane as an internal OpenAI-compatible proxy (§3.8). The brain also has a
  client-side token bucket for standalone runs (scenarios, the VPN scorecard).
  It honours `429` and `Retry-After`, and backs off (AIMD) on its own.
- **Front-load context deterministically.** Every agent's first message carries an
  *evidence pack* built by code, not discovered by tool calls: the diff hunks
  involved, the full text of the changed functions at base and head, every reference
  to the involved symbols with its enclosing function body, the relevant dependency
  source. Cap it (default 80K tokens, revisit after M1's soak results).
- **Encourage several tool calls per turn** (tool descriptions and system prompt say
  "batch independent reads in one turn"; `read` takes several paths).
- **Verify sequentially, not in parallel:** verifier A (the other model) first;
  verifier B only if A upheld. Refuted findings cost one verifier, not two.
- **Fewer, broader finders:** 6-7 lenses instead of ~10 (§6, M6).
- **A request budget per review**, allocated by stage, with hypotheses investigated
  in priority order. When the budget runs out, the rest is recorded as "not
  investigated: budget" — in the trace and in one line of the summary — never
  silently dropped.

Default budget for a typical MR (≈10 files, a few hundred changed lines),
configurable:

| Stage | Agents | Cap per agent | Expected total |
|---|---|---|---|
| Understand | 1 | 10 | ~8 |
| Hypothesise (finders) | 6-7 | 10 | ~50 |
| Investigate | ≤ 10, by priority | 14 | ~100 |
| Verify A (other model) | per confirmed finding | 10 | ~30 |
| Verify B | only if A upheld | 10 | ~20 |
| Reverse audit | 1 round, a 2nd only if the 1st found something | 10 | ~10 |
| Judge and write | 1 | 5 | ~4 |
| **Cap** | | | **300** |

A thread event (reply or re-check): cap 30. A cartographer run: cap 300, background
priority, nightly.

**What that means for time** (replaces design §8):

- A typical MR: ~250 requests ≈ 10 minutes of quota; **15-30 minutes** wall clock if
  nothing else is queued.
- A reply or re-check: ~25 requests; **2-5 minutes**, ahead of any review in the
  queue.
- **Throughput is the real limit:** roughly 4-6 typical reviews per hour for the
  whole service. If the team labels more MRs than that, the queue grows. Say so in
  the summary note ("queued, position 3") and tell the owner; more keys or a higher
  RPM is the fix, not a smaller review.

### 3.2 No model in the development loop

You cannot reach the gateway, and the owner can only paste short outputs back. So:

- **Mechanics are tested offline**, exhaustively:
  - a scripted **fake gateway** (OpenAI-compatible) that replays tool calls,
    reasoning, malformed arguments, empty and degenerate turns, and 429s;
  - **chat-template rendering**: every message list we would send is rendered
    through the real Qwen3.8-Flash-Next and GLM-5.3-Flash templates in
    `tests/fixtures/chat_templates/` (Jinja2). That catches a mid-conversation
    system message, reasoning in the wrong field, or a null `content`, without a
    gateway.
- **Quality is tested on the VPN, rarely and compactly:**
  - M1, the gateway probe — **ask once, early**;
  - optional scorecard checkpoints after M6 and M7 — **offer them, do not assume**;
  - the owner's final test.

  Each prints ≤ 20 lines and leaves full traces on the VPN machine.
- **Prompts are written carefully and generically.** Do not "tune" them against an
  imagined model. Never put scenario- or corpus-specific examples in a prompt
  (ROADMAP *Key decisions*: a prompt that names its test set is fitted to it).
- **Do not block on the owner.** If the probe result has not come back, use the
  template-derived defaults in §4.3; they live in one config file and change in one
  place.

### 3.3 The model layer

Per-model behaviour lives in one file, `habodek/brain/llm/models.yaml`. The gateway's
aliases map to these profiles. Rules from the chat templates (design §4.2, survey
`models.md`):

```yaml
profiles:
  qwen3.8-flash-next:
    reasoning_echo: every_assistant_turn   # template preserve_thinking=true
    reasoning_field: reasoning_content
    effort: {param: reasoning_effort, allowed: [xhigh, medium, low], default: xhigh}
    never_send: [thinking]                 # LiteLLM maps it to values the template rejects -> 400
    system_messages: first_only            # template raises on a later one -> 400
    sampling: {temperature: 1.0, top_p: 0.95, extra_body: {top_k: 20}}
  glm-5.3-flash:
    reasoning_echo: every_assistant_turn   # clear_thinking=false
    reasoning_field: reasoning_content
    effort: {param: reasoning_effort, allowed: [low, high, max], default: max}   # unknown -> max silently
    thinking: always_on                    # cannot be disabled
    sampling: {temperature: 1.0, top_p: 0.95}
common:
  tool_call_turn_content: ""               # never null (GLM rendered "None" -> loops)
  validate_tool_args_before_history: true  # vLLM silently turns bad args into {}
  degenerate: {retries: 2, then: other_model}
```

Degeneration: `finish_reason=length` with empty content, a reasoning run of repeated
characters, or a turn that announces a tool call and makes none. Retry the turn with
fresh sampling (twice); then hand the run to the other model; record it.

Model roles are a hypothesis until M1 and the scorecards say otherwise (design
§4.3): **GLM-5.3-Flash investigates; Qwen3.8-Flash-Next runs the finders and verifier
A.** Roles are configuration, never code.

### 3.4 Code intelligence for Python

- **Tier 1** (any language): ripgrep + tree-sitter tags, with definitions,
  references (classified, with enclosing function), callers, outline, the change map
  (base vs head syntax trees), and a personalised repo map adapted from Aider's
  `repomap.py` (Apache-2.0; keep its notice).
- **Tier 2 for Python: `jedi`** (pure Python, MIT, verified offline). Precise
  definitions and references, and it resolves into site-packages, which is where
  the internal libraries live (§3.5). `basedpyright` (with its bundled Node, a 61 MB
  wheel) is the alternative if jedi is too slow on a large repo; measure before
  switching.
- **C/C++:** not now. The tree-sitter pack already parses `c` and `cpp`; keep the
  language seam (one backend per language) so clangd can slot in later.

### 3.5 Dependency source: the venv and Artifactory

For a Python project at a given commit:

1. Read the pinned versions from the repo at that commit: requirements files,
   `pyproject.toml`, and `uv.lock` / `poetry.lock` / `pdm.lock` if present.
2. For each internal package (a configurable name pattern or list), get its source
   at that version:
   - from site-packages of the configured venv(s), if that exact version is
     installed;
   - otherwise `pip download --no-deps --index-url $HABODEK_PIP_INDEX_URL
     pkg==version` into a cache (`deps-cache/<pkg>/<version>/`) and unpack. A wheel
     is a zip; an sdist is a tar.
3. **A version bump is a change to review.** When base and head pin different
   versions, unpack both and give the reviewer the library diff (scenario 2).
4. Reverse dependencies — an MR *in* a library, whose consumers live in other
   repositories — come later (M10, cartographer). Until then, review a library MR
   on its own plus its tests, and say so in the summary.

The GitLab "materialise `project@ref`" path from the design (§6.3) is kept for
non-Python dependencies and reverse dependencies. Do not build it before M10.

### 3.6 The sandbox: sibling containers

Repository code runs only in throwaway containers that the **control plane** starts
(it owns the Docker socket; the brain never does):

- **env-prep:** creates the repo's venv from its lockfile, with `pip` pointed at
  Artifactory, on the VPN's internal network. Cached by hash(lockfile + Python
  version).
- **run:** `--network none`, read-only image, a throwaway copy of the worktree, the
  cached venv mounted read-only, CPU, memory and time limits, a cleared environment.

The brain asks for both through internal endpoints.

### 3.7 The engine bake-off is optional now

Running Qwen Code and the OpenHands SDK as challengers (design §5.3) needs the real
model, so it would cost the owner several VPN runs plus offline packaging of two more
systems. Keep the `Engine` interface, so the option stays open. **Do the bake-off only
if the M6 scorecard shows our loop failing in ways the traces attribute to the loop**
(lost thread, malformed calls, premature stops), not to the model's judgement.

### 3.8 The control plane gains an LLM proxy

`POST /internal/llm/v1/chat/completions` in the Go service:

- **Forwarding.** Forwards to LiteLLM with the gateway key; the brain never holds it.
- **Rate limiting.** Applies the global limiter and priority (§3.1). Priority comes
  from a header the control plane assigned to the job.
- **Logging and replay.** Writes every raw request and response to the job's trace
  directory. These recordings are the replay cassettes, and they work for any engine.

Brain subprocesses get `HABODEK_LLM_BASE_URL` pointing at it and a per-job token.

---

## 4. Verified facts — do not redo these

Checked on 2026-11-24.

### 4.1 Python packages, offline

- `tree-sitter-language-pack==0.13.0`
  - Ships abi3 wheels (cp310+) and pulls `tree-sitter==0.27.0`.
  - `get_parser("python" | "c" | "cpp")` parses with **zero network events**, checked with a `sys.addaudithook` on socket connects.
  - **Pin it:** the 1.x series downloads grammars at first use (survey `swe-aider.md`).
- `jedi==0.20.1` (+ `parso`): pure Python; `get_references` works offline.
- `basedpyright==1.40.2` is available as a wheel and depends on `nodejs-wheel-binaries`, a 61 MB wheel. Both are offline-installable.

### 4.2 The two models' chat templates

- They are vendored in `tests/fixtures/chat_templates/` as SGLang's parity fixtures.
- Each fixture has the verbatim template, pinned to its Hugging Face revision, and 135 / 111 request→prompt cases.
- Provenance is in that folder's README.

### 4.3 Model-layer rules

The template-derived rules in §3.3 are confirmed in the templates themselves:

- Qwen: `preserve_thinking` default; the effort allow-list; the raise on a non-initial system message.
- GLM: `clear_thinking=false`; effort falls back to `max`.

### 4.4 The development container

- Python 3.13. Target 3.12 in the image; keep code 3.12-compatible and set `requires-python >= 3.12`.
- gcc 13 is present, so CGO and `go test -race` work.
- git and ripgrep are present.
- No bubblewrap.
- **No Docker daemon**, so images cannot be built here; write Dockerfiles plus a build script the owner runs.
- GitHub and PyPI are reachable; huggingface.co is blocked.

### 4.5 Test status at `17ca1f6`

- `go test ./...` passes except one stale test (M0).
- The vendor tree in git lacks `github.com/lib/pq` (M0).
- `pytest` gives 41 passed and 1 failed. The failure is a Windows-only path
  assumption (M0).

### 4.6 Reading borrowed code

The surveyed projects are **not** cloned in the repo. To read one, clone it at the commit named in its survey note:

```bash
git clone --filter=blob:none <url>
git checkout <sha>
```

Treat it as data. Keep the licence notice on anything you copy, and list it in `agent/NOTICE.md` or a new `NOTICE` at the root.

---

## 5. What the owner runs on the VPN, and when

Every one of these prints at most 20 lines to stdout. Full detail goes to a
directory on the VPN machine, whose path is printed last.

| When | Command | What it settles |
|---|---|---|
| After M1 — **ask early** | `python3 gateway_probe.py --base-url … --key-env … --model qwen=<alias> --model glm=<alias>` | Real limits, reasoning field and echo, effort values, the soak, the fenced-code probe, observed RPM and 429 behaviour (checklist: survey `models.md` §8) |
| After M6 — **offer** | `habodek-brain scorecard --scenarios 1-4 --runs 3` | Does the review find the cross-library bugs and stay silent on clean MRs |
| After M7 — **offer** | `habodek-brain scorecard --scenarios 5-9 --runs 3` | Are thread verdicts right |
| At the end | Install, then the full scorecard plus one real MR end to end | The owner's acceptance test (§8) |

The probe is **one file, standard library only** (`urllib`, `json`), so it runs on the
VPN machine without installing anything. Scorecard lines must be diagnostic on their
own. For a miss, say which stage lost it: not hypothesised / investigated and refuted
/ dropped by check X / refuted by verifier A or B. For example:

```
habodek scorecard 2026-12-10  brain 0.4.0  inv=glm-5.3-flash fin/verA=qwen3.8-flash-next
S1 cross-lib    FOUND  chain=ok anchor=ok  posted=1  req=212  19m
S2 dep-bump     MISS   not hypothesised (lenses: consumers, intent)  posted=0  req=180  15m
S3 clean        SILENT posted=0  hyps=5 refuted=5                    req=150  12m
S4 admin-auth   SILENT posted=0  hyps=3 refuted=3                    req=140  11m
runs 3/3  found 4/6  silent 6/6  malformed=3  degenerate=1  429=12  rpm_obs=24.6
details: /var/lib/habodek/scorecards/2026-12-10T10-03/
```

---

## 6. Repository layout for new code

```
habodek/                      the Python package (today: eval CLI + diff parsing)
  brain/
    cli.py                    habodek-brain: review | thread | map | replay | probe | scorecard | local-job
    contract.py               pydantic models for job.json / result.json, JSON Schema exported
    config.py
    llm/                      client.py (httpx), shaping.py (models.yaml rules),
                              limiter.py, fake_gateway.py (scripted, for tests)
    runtime/                  loop.py, compaction.py, guards.py, tool.py (base + schemas)
    codeintel/                workspace.py, files.py (read/grep/glob), ts_index.py (tags,
                              cache by blob sha), refs.py, callers.py, changemap.py,
                              repomap.py (from Aider), jedi_backend.py, pydeps.py (§3.5)
    workflow/                 pipeline.py (stages), budget.py, evidence.py (packs, quote
                              checks, anchor-from-snippet), roles/ (one prompt template per
                              role, plain Markdown/Jinja2), judge.py, comment.py
    thread/                   linemap.py (map lines through diffs), agent.py
    memory/                   card.py, lessons.py, recall.py (BM25)
    trace/                    writer.py, view.py (static HTML), replay.py, scorecard.py
probe/gateway_probe.py        stdlib only, ≤ 20 lines of output
eval/scenarios/               generator + 9 scenario specs + expected outcomes +
                              Claude Code reference reviews
tests/                        pytest; tests/fixtures/chat_templates/ already present
agent/                        the Go control plane, kept and slimmed in M8
docker/                       Dockerfiles (control, brain, runner) + build scripts
```

One Python project (`pyproject.toml` at the root), with a new script entry
`habodek-brain`. Update the project description; it still says "evaluation
harness". Dependencies are small and each needs an offline check:

- pydantic, httpx, jinja2, pyyaml;
- tree-sitter-language-pack==0.13.0, jedi, networkx;
- rank-bm25 or a 40-line BM25 of our own.

**Do not add the LiteLLM library**: we talk to the LiteLLM proxy over HTTP, and the
library fetches a cost map from GitHub at import.

---

## 7. Milestones

In this order. Each ends with:

- tests green: `pytest -q`, `ruff check .`, and on the Go side `go build`, `go vet`, `go test -race`;
- a commit whose message says what was done and why;
- an update to the top of `ROADMAP.md`, including anything learned that contradicts this file.

The old Go brain keeps working until M8 replaces it; the owner may still be using it.

### M0 — Repository hygiene
- Fix `.gitignore` so the vendor tree is committed whole. The root `lib/` rule is a
  Python-template leftover: anchor it to `/lib/`, or add `!agent/vendor/**`.
- Re-vendor (`cd agent && go mod vendor`) and commit `vendor/github.com/lib/pq`.
- Fix `agent/internal/pkg/store/file/job_store_test.go:283`: `UpdateCompleted` now
  takes a `domain.ReplyVerdict`.
- Fix `tests/test_eval.py::test_posix_and_windows_paths_compare_equal`, which fails
  on Linux: `eval/score.py:222` normalises with `Path(...).as_posix()`, which leaves a
  backslash alone on POSIX. The suite was written on Windows. Replace `\` with `/`
  explicitly.
- **Done when:** from a *fresh clone*,
  `GOFLAGS=-mod=vendor GOPROXY=off GOMODCACHE=$(mktemp -d) go build ./...` succeeds,
  `go vet ./...` and `go test -race ./...` are clean, and `pytest`/`ruff` are clean.

### M1 — The gateway probe (then hand it to the owner)
- `probe/gateway_probe.py`, stdlib only, ≤ 20 lines out, raw log to a file.
- Checks, per model:
  - `/v1/models` and `/model/info` (context and output limits);
  - where reasoning comes back (`reasoning_content`, `reasoning`, inline `<think>`);
  - each effort value;
  - echo-back accepted;
  - a system message after turn 1 rejected (Qwen);
  - `content: null` vs `""` on tool-call turns;
  - three parallel reads;
  - a ~30-turn tool soak to ~150K tokens, counting malformed and degenerate turns;
  - the fenced-code phantom tool call;
  - the output cap;
  - observed rate-limit headers and 429 behaviour.

  The probe must stay under the RPM itself.
- Send the owner one short message: the command, the model aliases you need, and
  that the output is ≤ 20 lines.
- **Done when:** the probe runs end to end against the fake gateway (M2) and prints
  its summary. When the owner's output arrives, update `models.yaml` and §4.3 of this
  file.

### M2 — Brain skeleton and the LLM layer
- **Contract.** `contract.py` (job.json, result.json, finding with evidence chain), and the CLI skeleton.
- **Client.** `llm/client.py` over httpx, with `shaping.py` applying `models.yaml`:
  - reasoning echo, effort, sampling;
  - `content: ""` on tool-call turns, no late system messages;
  - argument validation before history;
  - degeneration detection.
- **Limiter.** `limiter.py`, a token bucket with priorities, `Retry-After` and AIMD.
- **Fake gateway.** `fake_gateway.py`: an httpx `MockTransport` for unit tests, plus a tiny HTTP server for CLI runs.
- **Trace writer.** Run dir, `events.jsonl` (every request and response, reasoning included).
- **Done when:** template-render tests pass for both models over multi-turn tool
  conversations with reasoning echoed. Recovery tests pass on the fake gateway:
  malformed arguments, empty turn, reasoning-only turn, degenerate turn, 429 storm.
  No test makes a real network call: an audit-hook fixture fails any connection
  other than the fake.

### M3 — Code intelligence (Python) and dependency source
- **Workspace.** Worktrees for head and base from a local repo path; the control plane does this in production.
- **Files.** `read` (whole file up to 2,000 lines / 100 KB, then paging; multi-path). `grep` (full regex via ripgrep, totals when truncated, refuses over-broad queries and says how to narrow them). `glob`, `outline`.
- **Symbols.** A tree-sitter tag index cached by blob SHA. `definition`, `references` (classified, with enclosing function and package), `callers(depth)`. Tier-2 refs via jedi behind the same interface. The result says which tier answered.
- **Change map.** Base vs head: functions and classes added, removed or modified; signature changes; changed exported names, constants and enum members.
- **Repo map.** Adapted from Aider, personalised to the change.
- **Dependency source.** `pydeps.py` (§3.5). Test it against a *local* fake index (a directory used with `--find-links`) holding two versions of a package.
- **Done when:** unit tests on small fixture repos cover each tool, including
  references across two packages and into a venv dependency, and a version-bump
  library diff.

### M4 — The agent runtime
- **Loop.** Native tool calls only. Each role ends in a schema'd terminal tool. Invalid arguments come back as a tool error; three strikes and the run ends `malformed`.
- **Budgets.** Turns, requests and wall clock.
- **Guards.**
  - A repeated identical call returns "already done".
  - Five identical calls, or N turns with no new read or note, force a checkpoint message.
  - Reasoning-only and empty turns get a targeted nudge.
  - On budget exhaustion the run must conclude.
- **Compaction.**
  - At ~60%, elide old tool *outputs*.
  - At ~80%, the model writes a structured summary: established facts with `file:line`, working hypotheses marked unverified, open questions, files read.
  - The task and the evidence are pinned. A `tool_calls` turn and its results are never split.
- **Engine.** The `Engine` interface (§3.7).
- **Done when:** loop tests on the fake gateway cover every guard and compaction
  path, and template-render tests still pass on compacted histories.

### M5 — Scenarios, scorer, scorecard, reference reviews
- Build the nine scenarios of §9 as real multi-repo git fixtures, generated by a
  script (like `eval/build_corpus.py`, deterministic). Also build a local fake index
  with the payments-lib versions.
- **Scorer:**
  - first half: found or missed, chain correct (the expected sites are cited), anchor on the right changed line, nothing posted on clean scenarios;
  - second half: the verdict, and the cited remaining site for `fix_incomplete` and `stands`.
- **`scorecard`:** runs N times per scenario and prints the ≤ 20-line format of §5.
- **Claude Code reference reviews.** You are Claude Code: review each first-half
  scenario as if asked "review this MR and post comments", and judge each
  second-half scenario as the thread agent should. Store the results as
  `eval/scenarios/<id>/reference_claude_code.md`. The scorecard can emit a
  side-by-side file for the owner to read on the VPN.
- **Done when:** the scorer passes on hand-written correct outputs and fails on
  wrong ones (chain missing a site, anchor off, a comment on a clean scenario), and
  the reference reviews are committed.

### M6 — The review workflow, end to end
The stages of design §6.6, under the budget of §3.1:

- **Gate.**
- **Understand.**
- **Finders: 6-7 lenses.** Each owes a receipt of what it walked ("traced 7 changed exports to 23 call sites"); a bare "nothing" is relaunched once, then recorded as unreviewed. Suspected criticals are never dropped here.
  1. intent and contracts, including removed behaviour (the `-` lines);
  2. consumers, both directions, including dependencies;
  3. correctness in the enclosing function, plus language pitfalls and sibling consistency;
  4. failure paths: errors, resources, concurrency, wrappers and caches;
  5. security and data;
  6. history and rules: `git log`/`blame` on touched lines, earlier threads, `.habodek.md`;
  7. optional: counter-frame — attacker, on-call and maintainer personas, and what the description does not mention.
- **Coverage gate from the traces.**
- **Triage.**
- **Investigators with evidence packs.**
- **Mechanical checks.**
  - Every quote exists at its path and revision.
  - The anchor is resolved *from the quoted snippet* against the diff hunks.
  - An off-diff finding must name the changed line that enables it.
  - One finding per root cause.
- **Verifier A, then verifier B** (fail closed).
- **One reverse-audit round.**
- **Judge and writer**, then `result.json` with comments and the summary.
- **Done when:** with the fake gateway driving scripted "good model" behaviour, the
  pipeline produces the expected `result.json` on all four first-half scenarios. A
  scripted "bad model" that invents a quote, anchors off-diff, or claims without
  evidence is stopped by the checks. Every drop appears in `findings.json` with its
  stage and reason.
- **Then:** offer the owner the S1-S4 scorecard (§5).

### M7 — The thread agent
Design §6.7:

- Starts from the stored finding: claim, chain, scenario, the head SHA it was made at.
- Maps every line through `diff(finding_sha, HEAD)`.
- Classifies the claim with the model.
- Re-walks the chain, goes where the developer points, and checks every site.
- Concludes `fixed` / `fix_incomplete` / `stands` / `withdrawn` / `need_info`.
- The other model attacks the conclusion.
- Budget 30.

**Done when:** scenarios 5-9 pass on the fake gateway with scripted behaviour; line
mapping is unit-tested on edits above, inside and below the anchor. **Then:** offer the
S5-S9 scorecard.

### M8 — Into the control plane (Go)
**New pieces:**
- The LLM proxy with limiter, priorities and raw logging (§3.8).
- The brain subprocess runner: it writes `job.json`, runs `habodek-brain`, and reads `result.json`.
- A durable Postgres queue.
- A finding store keeping full evidence chains and `head_sha`.
- Per-thread single-flight: a reply and a re-check never both answer; a newer event supersedes a queued one.
- **Triggers.** A reply in a bot thread starts the thread agent, debounced a minute. A push that touches any file in a finding's chain starts a re-check.

**Posting**, with PR-Agent's GitLab lessons (survey `pragent-hermes.md`):
- Position rules: an added line sets `new_line`; a context line sets both.
- Before posting, check that the MR's diff refs still match the reviewed SHAs.
- If GitLab rejects a line, post a file-level thread instead.
- Idempotent writes: a hidden `<!-- habodek:finding:<id> -->` marker, checked before any retry.
- An "in progress" note when a review starts. Never resolve threads.

**Removal:** delete the old brain: `internal/core/review/{structured_review,verdict,planner,chunk,risk}.go`, `internal/core/agents/`, `internal/core/prompt/`, `internal/pkg/tools/`, and `internal/core/reply/intent.go`, along with their env vars. Keep `cmd/cli review` as a dry run that drives the brain.

**Done when:** end to end on the local GitLab rig with the fake gateway: label → in-progress note → threads with chains → reply → verdict. `agent/NOTICE.md` and `agent/README.md` updated.

### M9 — Sandboxed execution
- Env-prep and run containers (§3.6) behind control-plane endpoints.
- A `run(cmd)` tool for investigators and verifiers.
- A per-repo recipe ("how to test this repo"), from `.habodek.md` or the repo card.
- **Done when:** a scenario variant is confirmed by a failing test run in the
  sandbox, and a run cannot open a network connection (tested).

### M10 — Memory
- The cartographer writes the repo card into the `habodek-memory` git repo, with background priority and budget 300.
- `.habodek.md` is read from the *target* branch, scoped by directory, and additive only: it can never suppress a class of finding.
- Lessons come only from verified thread outcomes, with citations and counts.
- `recall` (BM25).
- Every run records the memory commit it used.
- Reverse dependencies (§3.5 step 4).

### M11 — Observability
- `habodek trace show RUN` renders static HTML: the hypothesis tree, tool calls, verifier arguments.
- `replay`, from the proxy's recorded traffic or from inputs, optionally from a stage with a different model.
- `probe RUN --hypothesis "…"`.
- Metrics per repo: acceptance by category, and automatic bar-raising when acceptance falls.

### M12 — Offline delivery and the egress test
- Dockerfiles for control, brain and runner.
- `docker/build_bundle.sh` for the owner's internet-connected machine. It produces images (`docker save`), a wheelhouse for CPython 3.12 manylinux x86_64, checksums and a licence report. Or, if the VPN Artifactory proxies PyPI and Docker, Dockerfiles that build on the VPN.
- **The egress test.** The full scenario suite runs with an audit hook (Python) and a network namespace (on the owner's side) that permit only the stub gateway; any other connection attempt fails the build.
- Update `HANDOFF.md` and `DEPLOY.md` for v2.
- **Done when:** the owner can install from the bundle by following `DEPLOY.md`.

---

## 8. Done, for the whole thing

The owner's final test on the VPN:

- Scenarios 1-2 are found with correct chains in at least 2 of 3 runs; 3-4 are silent in 3 of 3.
- Scenarios 5-9 get correct verdicts: 7 and 9 in 3 of 3, the rest in 2 of 3.
- One real MR goes end to end, with its trace viewable on the VPN.
- No outbound connection except to the gateway and GitLab.
- Replies are answered ahead of queued reviews.
- A reader comparing Habodek's comments with the Claude Code references would rather have Habodek's, or finds them equivalent.

---

## 9. The scenarios, specified

Python. Three repositories, each a real git repo with a few commits of history.
The history includes one past commit titled like a bug fix, for the history lens.
Each repo is realistic enough to need exploration: tens of files, a little router,
storage, jobs, and distractor code. No third-party runtime dependencies, so their
tests run in the sandbox without installs.

- **`platform-common`** (library, version 2.3.0)
  - `store/users.py: get_user(uid)` raises `errors.NotFound` when missing.
  - `cache/users.py: CachedUsers.get()` negative-caches only on `NotFound`.
  - `web/router.py`, a tiny router with blueprints and prefix middleware.
  - `web/auth.py: require_user` turns `NotFound` into a 404.
  - `web/admin.py: AdminBlueprint` registers `require_admin` on every `/admin` route.
- **`payments-lib`** (library; tags 1.4.2 and 1.5.0, both built as wheels into a local fake index)
  - In 1.4.2, `PaymentClient.charge()` raises `PaymentDeclined`.
  - In 1.5.0 it returns `ChargeResult(status="declined")` instead. Changelog: "no exceptions for control flow".
- **`orders-service`** (consumer; pins both libraries)
  - `api/profile.py` and `jobs/digest.py` call `get_user` and use `.email`.
  - `domain/checkout.py` relies on `PaymentDeclined` to cancel the order.
  - `api/refunds.py`, a v1 router, plus a `/api/v2` router that has ownership middleware.
  - `api/orders.py`, `jobs/import_orders.py`.

| # | The MR or event | Expected |
|---|---|---|
| 1 | MR in `platform-common`: `get_user` returns `None` instead of raising; its tests updated. | One comment on the changed `return None` line. The chain cites `cache/users.py` (misses no longer cached) and the consumers in `orders-service` (`api/profile.py`, `jobs/digest.py`: `AttributeError`, 500s). |
| 2 | MR in `orders-service`: `payments-lib==1.4.2` → `==1.5.0`, nothing else. | One comment on the bump line. The chain cites the library change between versions and `domain/checkout.py` (declined payments now marked paid). |
| 3 | MR in `orders-service`: quantity validation moved into `validation.validate_quantity`, all four call sites updated. The tempting "missing `None` check" is guaranteed by the schema layer upstream. | No comment. The summary says what was checked. |
| 4 | MR in `orders-service`: a new `/admin/refunds` handler on `AdminBlueprint`, no visible auth check. | No comment. The middleware is found and the route confirmed to be under it. |
| 5 | Finding F1 (scenario 1) is open; a push restores `raise NotFound`. | `fixed`, naming what changed. |
| 6 | F1 open; a push adds a `None` check in `api/profile.py` only. | `fix_incomplete`, citing `jobs/digest.py`. |
| 7 | F1 open; the developer replies "fixed" and pushes an unrelated change. | `stands`, showing the unchanged path. |
| 8 | Finding F2 is open: a refund fetched by id without an ownership check (insecure direct object reference, IDOR) at `api/refunds.py`. The developer replies "ownership is enforced by the `owns_resource` middleware", and it is registered on this route's prefix. | `withdrawn`, saying what convinced it. A lesson candidate is recorded. |
| 9 | Same F2 and the same reply, but the middleware is registered only on `/api/v2`, while this route is on v1. | `stands`, citing the router registration that leaves the route uncovered. |

F1 and F2 are fixture state: stored findings with evidence chains, plus thread
notes. The thread scenarios do not depend on the review having found them.

---

## 10. Do not

- **Prompts:**
  - do not let a long rule list grow inside one prompt; split it into another lens (Qwen Code measured that long checklists in one agent get skipped);
  - do not put scenario or corpus specifics in a prompt.
- **Dependencies and the network:**
  - do not add a dependency without an offline install check and the audit-hook test;
  - do not use the LiteLLM library;
  - do not let any component call out except to the gateway (through the proxy) and GitLab (through the control plane).
- **Testing:**
  - do not post to any real GitLab from scenario runs;
  - do not treat one run as evidence; scorecards run each scenario three times (ROADMAP F5.1).
- **Behaviour and code:**
  - do not resolve threads;
  - do not delete the old Go brain before M8 works end to end.
- **The owner:** do not ask them for anything long. One-line questions; ≤ 20-line outputs.
