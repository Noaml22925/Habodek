# Survey: PR-Agent and Hermes Agent

Clones: `<clones>/{pr-agent,hermes-agent}` (`--filter=blob:none`). Nothing was installed or executed. Paths below are relative to each repo root.

---

## 1. PR-Agent

### Identity
- URL: https://github.com/qodo-ai/pr-agent. It redirects to **https://github.com/The-PR-Agent/pr-agent**: both URLs gave the same HEAD in `git ls-remote`, and commit `a28a5f24` (2026-05-14) says "replace old qodo-ai/pr-agent refs with new the-pr-agent/pr-agent".
- Commit read: `48edc526e369bc2dc8cd70992661d66a3977621e`, 2026-11-23.
- 1,505 commits in the last 90 days. 272 distinct author emails in the last 180 days. The top authors are community maintainers (Peter Dave Hello 457, Utsab Dahal 247, Alex Tumanov 141), not Qodo staff.
- Latest tag: `v0.50.0` (2026-11-08). `pyproject.toml:7` still says `0.49.0`. The repo is **not archived**. It is "a community-maintained legacy project of Qodo" (README:46). Qodo is now only a "Gold sponsor". Its commercial product (formerly "Qodo Merge" / pr-agent-pro) is separate and closed. Code comments still point to `Codium-ai/pr-agent-pro` (`git_providers/github_provider.py:3354`).

### Licence
- Now **MIT** (`LICENSE`, "Copyright (c) 2026 The PR Agent").
- The licence has changed three times (`git log -- LICENSE`): Apache-2.0 (2023), then AGPL-3.0 (`70428ebb`, 2025-05-22), then Apache-2.0 (`fdbcecc8`, 2026-05-20), then MIT (`d7e94bc2`, 2026-07-09). If we vendor it, we should pin a post-July-2026 commit.
- No non-OSS code lives in this repo. No CLA file was found (CONTRIBUTING.md was not read in full: not verified).

### Language and footprint
- Python ≥3.12 (`pyproject.toml:21`). `uv.lock` pins 182 packages.
- Core dependencies include `litellm==1.110.1`, `openai`, `anthropic`, `tiktoken==0.14.0`, `langfuse`, OpenTelemetry, FastAPI, gunicorn and `dynaconf` (`pyproject.toml:35-80`). Cloud and vector-DB dependencies are extras.
- Mostly pure Python with standard binary wheels (tiktoken, pydantic-core, regex). A wheelhouse plus `uv sync --offline` should work. Not verified by building.

### Outbound calls and telemetry
- **LiteLLM cost map.** LiteLLM fetches its model-cost JSON at import unless `LITELLM_LOCAL_MODEL_COST_MAP=True`. The Dockerfiles set this (`docker/Dockerfile:10`). A pip install must set it by hand.
- **tiktoken.** Encodings are pointed at LiteLLM's bundled files, so tiktoken does not download them (`algo/token_handler.py:19-38`).
- **OpenRouter context lookup.** This GET (`algo/ai_handlers/openrouter_model_info.py:39-50`) only runs on OpenRouter routes. It is disabled by `LITELLM_OFFLINE_MODE=true` or by setting `config.custom_model_max_tokens`.
- **OTEL.** Off by default (`settings/configuration.toml:569-577`, `telemetry/config.py:28`). The destination is operator-defined.
- **LiteLLM callbacks.** posthog, langsmith and others exist but are off by default (`configuration.toml:489-494`, `algo/ai_handlers/litellm_helpers.py:45`).
- **Ticket fetches** (Asana, Linear) and **`--extra_config_url`** only happen when configured (`commands/ticket_pr_compliance_check.py:681,801`, `cli.py:205`).
- No analytics or phone-home code was found. The "analytics" logger only writes to a local folder (`log/__init__.py:85-91`).

### LLM layer
- All calls go through LiteLLM as a library. An OpenAI-compatible gateway is configured with `OPENAI__API_BASE`/`OPENAI__KEY` (docs `usage-guide/changing_a_model.md:68-72`). Unknown models need `config.custom_model_max_tokens`; otherwise token-limit resolution raises (`algo/token_budget.py:153-292`).
- **No function calling at all.** The model returns YAML (`pr_reviewer.py:1894-1900`), which is parsed with key-repair heuristics.
- Only `choices[0].message.content` is read (`litellm_ai_handler.py:3945`). `reasoning_content` is ignored, which does no harm because every call is single-shot.
- Qwen and GLM are covered by token-limit entries only (`algo/__init__.py:305,339`), plus `zai.glm-5` in `no_temperature_models` (`configuration.toml:144`) and a documented `extra_body` with `chat_template_kwargs.enable_thinking` (docs `changing_a_model.md:103-110`).
- Fallback model chain: `algo/pr_processing.py:921-975`.

### Headless mode
- Run as `python -m pr_agent.cli --pr_url=<MR URL> review`. `--json-output <file>` writes results and token usage. `--output` writes the Markdown. `--diff-file`/`--stdin` select a plain-diff mode (`cli.py:160-228`). Non-zero exit on failure (`cli.py` around 370-410).
- Also available: a GitLab webhook server (`servers/gitlab_webhook.py`) and an A2A server ("Mosaico", `mosaico/server.py`).
- There is no interactive part, so nothing needs auto-approving.

### Agent loop: there isn't one
- `algo/skills_loader.py:24-27` says so in its own words: "PR-Agent runs single-shot model calls and has no tool-use loop".
- `grep tool_calls|tools=|tool_choice` over `pr_agent/` returns nothing. There is **no repo exploration**.
- What it adds around the single call:
  - static `AGENTS.md`/glob context files, read via the provider API (`algo/repo_context.py`, `configuration.toml:43-53`);
  - a deterministic "structural reach" row read from a build-graph JSON file (`algo/structural_reach.py:1-6`);
  - regex risk checks (`algo/deterministic_risk.py:1-4`).
- The prompt tells the model it is blind outside the diff: "you only see changed code segments… Do not speculate… unless you can identify the specific affected code path" (`settings/pr_reviewer_prompts.toml:17,23`).

### Prompt building and the "PR compression" token budget
- **`/improve` no longer exists as a separate pipeline.** `commands/pr_code_suggestions.py` and its self-reflection prompts were deleted on 2026-11-19 (`ed3ae0eb`). `/improve` now runs `PRReviewer` ("/improve runs /review since 0.51", `agent/command_registry.py:67-75`). This is unreleased: v0.50.0 still has the old `/improve`.
- **Prompt variables:** `commands/pr_reviewer.py:396-450` (title, branch, description, commit messages, repo_context, skills_context, previous_findings, tickets). Rendered with Jinja2 StrictUndefined.
- **Budget:** `context_window − output_reserve − fixed prompt tokens` (`algo/token_budget.py:533-551`).
  - Soft and hard output buffers are 1,500 and 1,000 tokens (`algo/pr_processing.py:40-41`).
  - `max_model_tokens=256000` caps everything (`configuration.toml:57`).
  - Counting uses tiktoken `o200k_base` for non-GPT models, plus `model_token_count_estimate_factor=0.3` when an accurate count is requested (`token_handler.py:100-104,308-366`; `configuration.toml:64`).
- **`get_pr_diff` (`pr_processing.py:211-396`):**
  1. Render every file's patch, extended with context, as `__new hunk__` (with line numbers) and `__old hunk__` (`git_patch_processing.py:404-534`).
  2. Extra context is `patch_extra_lines_before=5` and `after=1`, capped at 10 (`configuration.toml:68-71`, `pr_processing.py:42,195-208`).
  3. "Dynamic context" walks up to 10 lines back to the enclosing function/class header named in the `@@` section header (`git_patch_processing.py:183-209`).
  4. If the whole set fits the soft budget, it is returned as is.
  5. Otherwise it is **compressed** (`pr_processing.py:748-838`). Extra context is dropped, deletion-only hunks are removed (`git_patch_processing.py:330-401`), and deleted files are reduced to a name list. Files are sorted by language, then by token size (largest first), and greedily packed until the soft budget (`generate_full_patch`, `pr_processing.py:841-911`). Leftover files are listed by name only, under "Additional modified files (insufficient token budget…)" (`pr_processing.py:33-37,339-375`).
- **Large-PR chunking** (opt-in `enable_large_pr_chunking`, up to `max_number_of_calls=3`; `configuration.toml:231-232`) reviews chunks separately. The results are merged by fixed rules: findings are unioned; judgements (risk, score) take the most conservative value (`algo/review_merge.py:1-20`; `pr_reviewer.py:1623-1736`).

### Self-reflection and scoring
- **Current `/review`:** an optional `confidence: 0-10` field in the schema. Its rubric runs from "9-10 verified from visible code" down to "5-6 plausible but unverified" (`pr_reviewer_prompts.toml:80`). Findings below `min_finding_confidence` are dropped (`pr_reviewer.py:2376-2395`; default 0 = off, `configuration.toml:215`).
- **Deterministic checks before a fix becomes a committable suggestion:**
  - the line range must be in the diff (`algo/suggestion_checks.py:73-108`);
  - `existing_code` must match the file (`validate_suggestion`);
  - re-indentation is applied;
  - a Python fix must still compile (`suggestion_checks.py:138`);
  - a fix of at most 10 lines is offered as committable (`pr_reviewer.py:2397-2469`).
- **Old `/improve` (≤ v0.50):** a second model call scored each suggestion 0-10 and re-derived its line numbers. The rubric caps "verify/ensure" suggestions at 7 and error-handling ones at 8, and gives 0 for docstrings or "questions about entities defined elsewhere". See `git show ed3ae0eb^:pr_agent/settings/code_suggestions/pr_code_suggestions_reflect_prompts.toml` and old `pr_code_suggestions.py:1293-1300,1381-1400`.

### GitLab provider (directly reusable knowledge)
- **Inline comments** (`git_providers/gitlab_provider.py:2587-2646`):
  - `mr.discussions.create({'body', 'position'})`, where position is `position_type:'text'` with `base_sha/start_sha/head_sha`, `new_path/old_path`;
  - an added line sets `new_line` only, a deleted line sets `old_line` only, a context line sets **both**;
  - the SHAs come from `mr.diffs.list(per_page=1)[0]` and are **checked to match the reviewed refs**; otherwise the comment is skipped (`get_relevant_diff`, 2776-2797).
- **Anchoring by position, not text search:** the anchor is classified by walking hunk headers, because a content search mis-anchors repeated lines (`publish_code_suggestions`, 2844-2987, in particular 2900-2935). Multi-line suggestions use the syntax ```` ```suggestion:-0+N ```` (2893).
- **Fallbacks:** if GitLab rejects the position (the line is not in the diff), a **file-level resolvable thread** is posted (`position_type:'file'`), then a plain note if that fails too (2648-2682, 2684-2774).
- **Idempotent writes:** `_suggestion_write_may_have_landed` decides from the error class and HTTP status whether a timed-out write may have been created, so it is not re-posted as a duplicate (2556-2585).
- **Draft-note batching:** `gitlab.publish_code_suggestions_as_review` (2866-2869, 2820-2842). Drafts GitLab could not anchor come back with `line_code: null`; they are deleted, because `bulk_publish` before GitLab 19.6 fails on them.
- **Replies:** `mr.discussions.get(id).notes.create` (2505-2508). The webhook appends `--comment_id=<discussion_id>` to note events (`servers/gitlab_webhook.py:666-685`).
- **Outdated-thread auto-resolve** (opt-in `resolve_outdated_inline_threads`). A thread qualifies only if all of these hold (2264-2303, helpers 165-273):
  - the bot opened it and every note in it is the bot's;
  - it is not already resolved;
  - its recorded `head_sha` is not the current one;
  - the flagged line was actually removed in base..head. A line that merely shifted stays open.
- **Cross-run deduplication:** SHA-256 fingerprints of body and code are hidden as HTML-comment markers (`algo/inline_comment_dedup.py:1-30,100-125`).
- **Finding lifecycle** (ACTIVE/RESOLVED/DISMISSED) is stored as a JSON marker in the summary comment (`algo/review_finding_state.py:19-23,194-291`). It is fed back into the next prompt with the rule "repeat word-for-word if still present; dismissed = reviewer closed it, `reply` gives the reason" (`pr_reviewer_prompts.toml:358-367`; dismissed threads are read in `pr_reviewer.py:1310-1336`).

### Other items
- **Context compaction, sub-agents:** N/A.
- **Memory:** per-repo `.pr_agent.toml` and `AGENTS.md` only.
- **Sandboxing:** N/A. The model never executes anything. `/help_docs`, which cloned repos, is disabled for security (`command_registry.py:86-88`).

### Fit for Habodek
- **As the engine: no.** It is a better-engineered version of what we already have (single-shot diff-in, YAML-out) and does not meet the quality bar: no tool loop, no caller-following, no hypothesis testing. That is the biggest blocker.
- **As a source of ideas: excellent, especially for the GitLab layer.**

### Ideas worth stealing
1. GitLab position rules, SHA-match guard, and file-level-thread fallback (`gitlab_provider.py:2587-2797`).
2. "May have landed" write classification for idempotent posting (`gitlab_provider.py:2556-2585`).
3. Outdated-thread resolution only when the flagged line was removed, not shifted (`gitlab_provider.py:165-273,2264-2303`).
4. Finding-state ledger with word-for-word re-emission and dismissed-with-reply context (`review_finding_state.py`, `pr_reviewer_prompts.toml:358-367`). This maps onto our "re-check threads after push" and "by design" replies.
5. The `__new hunk__`/`__old hunk__` line-numbered diff format plus enclosing-function dynamic context (`git_patch_processing.py:183-209,404-534`), as the initial context handed to an agent.
6. A confidence rubric tied to verification level (`pr_reviewer_prompts.toml:80`) and the deterministic suggestion validation (`suggestion_checks.py`).
7. Chunk-merge rules: union findings, most-conservative judgement (`review_merge.py:1-20`).

---

## 2. Hermes Agent (Nous Research)

### Identity
- URL: https://github.com/NousResearch/hermes-agent. The guessed URL resolved directly (`git ls-remote` returned HEAD); README and LICENSE confirm Nous Research.
- Commit read: `352b249f67fb6a4a7d7904acaca9dd410db229e0`, 2026-11-23.
- Activity is extreme:
  - 50,759 commits in the last 90 days (17,794 in the last 30);
  - 5,280 author emails in the last 180 days;
  - 11,328 commit messages in 90 days mention a Claude/Codex co-author, so this is heavily agent-written;
  - 76,174 commits since 2025-07-22.
- Releases: `v0.23.3` (2026-11-20). There are also daily `canary.*` tags and older CalVer `v2026.9.x` tags.
- Size: 27,552 files; `agent/` alone has 409 modules.

### Licence
- **MIT** ("Copyright (c) 2025 Nous Research", `LICENSE`).
- `AGENTS.md` rejects "third-party products in the core tree" (they ship as plugins). The Nous Portal, hosted tools and the Nous telemetry endpoint are services, not code.
- No CLA was found (not verified exhaustively).

### What it is
- A **general personal agent**, not a coding agent. It runs one agent core behind a CLI, TUI, Electron desktop, web dashboard, ACP editor server, and a messaging gateway with 25+ platforms (Telegram, Slack…) (`AGENTS.md` "What Hermes Is"; `plugins/platforms/`).
- Coding is one "posture" (`toolsets.py:61-63`). There are also cron, kanban, browser/CDP, computer-use, TTS and image generation.

### Language and footprint
- Python `>=3.11,<3.15`; core dependencies are gated on `python_version >= '3.14'` and exact-pinned (`pyproject.toml:15` and the `dependencies` block). `uv.lock` has 355 packages including extras.
- Node/pnpm is needed for the TUI and desktop (`pnpm-lock.yaml`), but not for headless use.
- It has its own package manager "PM" (`pm/`), which installs Python, Node, ripgrep and so on. The installer is a `curl | bash` script (README).
- Vendoring offline is possible in principle (lockfiles exist; uv2nix via `flake.nix`) but heavy. Not verified.

### Outbound calls (partial list; the repo is too large for an exhaustive audit)
- **models.dev catalogue**: `https://models.dev/api.json` (`agent/models_dev.py:27`).
  - Network is used only when no disk cache exists; a stale cache triggers a background refresh with a 5-minute back-off (`models_dev.py:1-8`).
  - There is **no off switch**, only a mirror URL `models_dev.url` (`models_dev.py:278-283`).
  - Offline, we would need to pre-seed `~/.hermes/models_dev_cache.json` and point the URL at an internal mirror. Not verified that failures stay silent.
- **Passive update check**: `updates.check: True` by default (`hermes_cli/config_defaults.py:2293-2295`; `hermes_cli/banner.py:275-287`). It can be set to false.
- **Shared-metrics telemetry** to `https://telemetry.nousresearch.com/v1/telemetry`: `enabled: False, send: False` by default, consent-gated (`config_defaults.py:2276-2285`; `hermes_cli/observability/shared_metrics_send_config.py:15`).
- **LSP auto-install**: `lsp.install_strategy: "auto"` runs npm/go/pip installs on first use (`config_defaults.py:2358-2360`; `agent/lsp/install.py:1-10`). Set it to `manual`, or `lsp.enabled: false`.
- **Lazy PyPI installs**: `security.allow_lazy_installs: True` (`config_defaults.py:1738-1741`). Set it to false.
- Plugin update checks against PyPI (`hermes_cli/plugins_updates.py:468`) and GitHub releases (`hermes_cli/source_releases.py:346`). Not verified when these fire.
- Bitwarden `bws` and `iron-proxy` binaries auto-download only when those features are enabled (`config_defaults.py:2430,2544`).
- OTLP export only goes to an operator endpoint (`agent/monitoring/otlp_exporter.py:1-7`). HF trace upload is user-initiated (`agent/trace_upload.py`).
- Default web_search/web_extract tools need external services. The `--toolsets` flag can restrict them.
- **Verdict:** it can probably be made offline through config, but there is no single offline switch, and with ~27k files a full audit is not done.

### LLM layer
- Uses the OpenAI SDK directly against `base_url`, with provider `custom` for any OpenAI-compatible endpoint (`cli-config.yaml.example:107,185-199`). LiteLLM proxies are a recognised route (`agent/agent_runtime_helpers.py:1252-1262`). LiteLLM-specific behaviour:
  - LiteLLM-style part-level cache markers are not sent (`agent/prompt_caching.py:29-66`);
  - the served model is read from `x-litellm-model-id` (`agent/served_model.py:1-20`);
  - error classification knows LiteLLM billing and invalid-body shapes (`agent/error_classifier.py:125,1264,1436`).
- **Native tool calling.** The loop dispatches `assistant_message.tool_calls` (`agent/conversation_loop.py:1764-1766`). Hermes-style `<tool_call>` text appears only when exporting training trajectories (`agent_runtime_helpers.py:92-137`) and in the ACP bridge for backends without a tools channel (`agent/acp_openai_bridge.py:1-30`).
- Malformed calls (`agent/turn_tool_validation.py:1-8`): unknown tool names are auto-repaired with a 3-strike exit; bad JSON arguments are retried, then answered with recovery tool results.
- **reasoning_content handling: the best of anything I have read.** Reasoning is stored as `assistant_msg["reasoning"]` (`agent/AGENTS.md`). It is **echoed back by default** on the carrier(s) each route reads (`agent/message_sanitization.py:660-760,946-990`):
  - a remote custom endpoint (our LiteLLM gateway) gets `reasoning_content` only (`route_reasoning_carriers`, 745);
  - "must-echo" routes (DeepSeek/Kimi/MiMo) get a `" "` pad when empty;
  - a field the gateway rejects is learned at runtime and dropped for that route (`record_reasoning_field_rejection`, 1094);
  - `reapply_reasoning_echo` reconciles history after a mid-conversation fallback.
- Qwen- and GLM-specific code:
  - GLM-5.3 is reasoning-mandatory, so a "reasoning disabled" request returns 400 and is recovered (`error_classifier.py:1410`, `turn_recovery.py:736`);
  - Z.AI/Zhipu reuse HTTP 429 for server overload (`error_classifier.py:185,1269`);
  - Qwen/vLLM's "No user query found" chat-template error is handled (`error_classifier.py:360`);
  - Qwen cache TTL is 5 minutes (`prompt_caching.py:124-146`).

### Headless mode
- `hermes -z "<prompt>"` (`--oneshot`): **stdout is the final response only** (`hermes_cli/main.py:2794-2800`). There is **no JSON event stream**.
- Exit codes: 0 ok, 1 empty, 2 failed/partial/budget, 130 interrupted (`hermes_cli/oneshot.py:95-111`).
- `--usage-file PATH` writes a JSON ledger: tokens, cost, `completed`, `partial`, `turn_exit_reason` (`oneshot.py:22-28,114-135`).
- Other flags: `-m/--model`, `--provider`, `--reasoning`, `-t/--toolsets` (restrict tools), `--yolo` (auto-approve dangerous commands), `--ignore-user-config`, `--ignore-rules`, `--safe-mode` (`hermes_cli/_parser.py:148-220`).
- Alternatives:
  - the Python API `AIAgent(...).run_conversation(msg) -> {final_response, messages}` (`agent/AGENTS.md`, `run_agent.py`), which is the most parseable;
  - the ACP JSON-RPC server `hermes-acp` (`pyproject.toml:596`);
  - the `tui_gateway` JSON-RPC.

### Agent loop
- A synchronous `while (api_call_count < max_iterations and iteration_budget.remaining > 0) or _budget_grace_call` loop (`agent/conversation_loop.py:1724-1773`). It is split into about 25 phase modules (`agent/turn_*.py`), each returning an action verdict (`_run_phase`, 1529-1547).
- Each API call has its own retry/recovery loop (`_run_api_retry_loop`, 1550-1579): rate-limit guard, then build, then call, then check, with typed error handlers and fallback-model restarts.
- Budgets:
  - `AIAgent` defaults to `max_iterations=500` (`agent/AGENTS.md`);
  - `agent.max_turns: null` means unlimited (`config_defaults.py:64-67`);
  - optional wall-clock budget and a budget-warning ratio.
- After the turn: background memory/skill review (see Memory) and a **verification stop gate**, which nudges the model when it tries to finish right after editing code without fresh check evidence (`agent/verification_stop.py:1-3`, `agent/verification_evidence.py:1-3`).

### Context management and compaction
- **ContextCompressor** (`agent/context_compressor.py:2663-2689`):
  - trigger at `threshold_percent=0.50` of the window, with per-model overrides and `threshold_tokens_cap`;
  - config floor of 0.75 for windows under 512K (`config_defaults.py:573-576`);
  - protects `protect_first_n=3` and `protect_last_n=20` messages, with a "lean" token-budget tail;
  - summary target 20%.
- **Stages:** old tool results are pruned deterministically first (stubbed, `agent/context_compressor_prune.py`). Then a structured summary is written by an auxiliary model. If that fails, a deterministic summary with fixed sections is used (Goal, Constraints, Completed Actions, Active State, Blocked, Key Decisions, Relevant Files, Last Dropped Turns; `context_compressor.py:3390-3420`).
- **Epistemic-stance rule:** uncertain claims must be listed under "## Unverified / Working Hypotheses" so compression does not harden them into fact (`agent/context_compressor_prompt_rules.py:1-21`). The LLM summary prompt text itself was not read.
- **Cache invariant:** the system prompt stays byte-stable for the whole session; compression is the only sanctioned cache break (`AGENTS.md`).
- **Tool output caps:**
  - `read_file` cuts at 100K characters on a line boundary and returns `next_offset` (`tools/file_tools.py:52-111`);
  - per-tool `max_result_size_chars` (`tools/registry.py:1119-1127`);
  - terminal output keeps a head/tail window and spills the full text to a file (`tools/terminal_tool.py:181,1446`).

### Tools
- Core list: `toolsets.py:11-36` (web, terminal/process, read/write/patch/search_files, skills, browser, todo, memory, session_search, clarify, execute_code, delegate_task, cron, kanban, computer_use). The coding posture drops TTS, image generation, cron and computer-use.
- `read_file` returns `LINE_NUM|CONTENT` with pagination (`file_tools.py:1182-1200`).
- `search_files` uses ripgrep (a PM-pinned binary, `tools/file_operations_search.py:122-130`) with content/files modes, context lines and `files_only/count` (`file_tools.py:1325-1345`).
- LSP is used for **post-edit diagnostics**, not exposed as find-references (`agent/lsp/`).
- No tree-sitter repo map and no git-blame tool was found; git goes through the terminal.
- `execute_code` runs Python scripts that call tools over RPC (README).
- `subdirectory_hints` appends a nested `AGENTS.md` to the tool result when the agent enters that directory (`agent/AGENTS.md`).

### Sub-agents
- `delegate_task` spawns child `AIAgent`s with a fresh conversation, their own `task_id` and terminal session, and the parent's toolsets minus `delegate_task/clarify/memory/send_message/cronjob_manage` (`tools/delegate_tool.py:1-11`; `tools/delegate_tool_toolsets.py:14-22`).
- Batch mode runs in parallel, default 10 concurrent children. Depth is 1 (flat) unless `max_spawn_depth` is raised (`tools/delegate_tool_config.py:19-23,197-207`). Each child has 250 iterations (`config_defaults.py:1310`).
- The parent sees only the summary. Summaries are sized to the parent's remaining context and overflow spills to a file (`delegate_tool_results.py:164,282`). An optional output schema is validated.

### Memory (analysed in depth, as asked)
- **Built-in store:** two bounded files, `MEMORY.md` (agent notes, 2,200 chars) and `USER.md` (user profile, 1,375 chars), under `<HERMES_HOME>/memories/`. That is **per-profile, not per-repo** (`tools/memory_tool.py:1-5,37-39`; `tools/memory_tool_store.py:177-195`).
- **Format:** entries separated by `\n§\n` (`memory_tool_store.py:26`).
- **Load:** at session start, entries are deduplicated, scanned for prompt-injection/exfiltration patterns (hits replaced by `[BLOCKED…]` in the prompt only), and frozen into a **system-prompt snapshot**. Writes during the session go to disk but never change the live prompt, to keep the cache intact (`memory_tool_store.py:223-258`).
- **Write path:** a single `memory` tool takes atomic batch `operations` (add/patch/remove), with the char limit checked on the final result (`memory_tool.py:370-410`).
  - Writes lock the file, re-read it, and refuse to rewrite if they detect "drift" (free text appended outside the `§` format), keeping a backup (`memory_tool_store.py:51-60,353-401`).
  - Each write is threat-scanned (`memory_tool_store.py:29-32,403-410`).
  - An optional approval gate stages writes for a human (`memory_tool.py:82-98`).
  - After 3 failed consolidations in one turn, it stops retrying so the reply is not blocked (`memory_tool_store.py:183-221`).
- **Who updates it:** a **background review fork**. After a turn it may spawn a daemon thread that replays the conversation in a forked `AIAgent`, using the same cached prefix and a whitelist of only the memory and skill tools (`agent/background_review.py:1-6,1212-1260,1460`).
  - The fork is triggered every `memory.nudge_interval=10` user turns (`agent/turn_context.py:818-827`; `agent/agent_init.py:1143,1177`) and every `skills.creation_nudge_interval=10` tool iterations (`agent/review_signals.py:34-40`; `agent_init.py:1219-1221`).
  - The review prompts (`background_review.py:446-656`) say: save only facts **grounded** in a message or tool result; one store per fact; procedures go into **skills**, not memory. The "do not capture" list excludes environment failures, negative claims about tools ("X is broken"), transient errors and unresolved dead ends.
- **Skills:** class-level `SKILL.md` plus `references/`, `templates/`, `scripts/`. `skill_manage` enforces read-before-write. A background curator consolidates skills (`agent/curator.py`).
- **Project context:** `AGENTS.md` chain from git root to cwd, `CLAUDE.md` in cwd, `.cursorrules`, `.hermes.md`. Loaded once at startup, capped and injection-scanned (`agent/prompt_builder.py:85-96,1623-1777`).
- **Other:** past sessions are searchable via FTS5 `session_search` (README; not read in code). There are external memory-provider plugins such as Honcho (`agent/memory_manager.py`, `memory_provider.py`). Sub-agents cannot write memory; cron sessions skip memory providers.
- **For per-repo memory:** run one `HERMES_HOME` profile per repository (profiles are isolated "islands", `AGENTS.md`). Not tested.

### Sandboxing
- Terminal backends: local, docker, ssh, modal, daytona, vercel_sandbox (`tools/terminal_tool_backends.py:281-283`; `tools/environments/`). **File tools also run through the backend's `execute()`** (`tools/file_operations.py:1-6,216-248`), so the Docker backend sandboxes reads and writes too.
- Docker hardening: `--cap-drop ALL` and `no-new-privileges`, PID/CPU/memory limits (`tools/environments/docker.py:1-5,325,396,810-818`). `terminal.docker_network: false` gives `--network=none` and refuses contradicting extra arguments (`docker.py:831-843`).
- There is an elaborate approval system for dangerous commands (`tools/approval*.py`); `--yolo` bypasses it.
- The local backend has no OS sandbox. No read-only mode was found (not verified).

### Code review support
- No MR/PR review mode and **no GitLab integration**. A `github` skill exists (`skills/software-development/github/`).
- `requesting-code-review` is a pre-commit self-review skill using an independent reviewer sub-agent ("No agent should verify its own work", `skills/software-development/requesting-code-review/SKILL.md:1-25`).

### Fit for Habodek
- **As the engine: no**, even though its LLM layer is excellent. The blockers, in order:
  - Its footprint, scope and churn: a personal-assistant platform of ~27k files with about 600 commits a day. We cannot audit, vendor or pin it with confidence, and its outbound surface (models.dev, update checks, LSP/PyPI lazy installs, web tools) needs per-feature switches with no global offline mode.
  - No structured event stream from `-z`.
  - Memory is per-profile and assistant-oriented.
- **As a source of ideas: very high**, especially the reasoning-replay policy for our Qwen/GLM-via-LiteLLM setup and the memory-curation discipline.

### Ideas worth stealing
1. The reasoning-carrier policy: echo `reasoning_content` on tool-call turns by default, pad must-echo routes, learn rejected fields per route at runtime, reconcile after fallback (`agent/message_sanitization.py:660-760,946-1110`).
2. Frozen-snapshot memory: bounded char budget, `§` entries, atomic batch ops, threat scan on load and write, drift guard (`tools/memory_tool_store.py`). A good template for per-repo reviewer memory ("this repo's conventions", "accepted by-design patterns").
3. Background review fork with a grounding rule and a "do not capture" list, so a reviewer does not learn "tool X is broken" or turn one-off developer replies into standing rules (`agent/background_review.py:446-656`).
4. The epistemic-stance rule in compaction: "## Unverified / Working Hypotheses", which fits hypothesis-driven review (`agent/context_compressor_prompt_rules.py`).
5. The verification stop gate, which nudges when the model tries to finish without evidence (`agent/verification_stop.py`). Adapt it as "do not post a finding you have not verified in code".
6. Sub-agent pattern: blocked tools, flat depth, summary sized to the parent's headroom, output schema (`tools/delegate_tool*.py`).
7. The `-z` exit-code contract plus usage ledger for pipelines (`hermes_cli/oneshot.py:95-135`).
8. Tool-call validation with auto-repair of unknown names, a 3-strike exit, and recovery as tool-role results so role alternation is preserved (`agent/turn_tool_validation.py`).

---

## Verdict summary (10 lines)
1. PR-Agent: MIT since 2026-07, moved to The-PR-Agent org, community-maintained, very active (v0.50.0); Qodo's commercial product is separate.
2. PR-Agent has **no tool loop and no repo exploration**: single-shot diff, YAML out. `/improve` was folded into `/review` on main (0.51, unreleased).
3. PR-Agent as engine: **reject**. It cannot reach the "Claude Code on the repo" bar.
4. PR-Agent as ideas: **adopt its GitLab know-how**: position rules, SHA guard, file-level fallback, idempotent writes, outdated-thread resolution, finding-state ledger.
5. PR-Agent offline: feasible (`LITELLM_LOCAL_MODEL_COST_MAP`, bundled tiktoken, telemetry off by default).
6. Hermes: MIT, a general personal agent (not coding-specific), enormous (27k files, ~50k commits in 90 days).
7. Hermes LLM layer is excellent for us: native tools, a default reasoning_content echo with runtime learning, LiteLLM/GLM/Qwen quirks handled.
8. Hermes memory is per-profile MEMORY.md/USER.md plus skills, curated by a background fork. Per-repo memory would need one profile per repo.
9. Hermes as engine: **reject**. Scope and churn are unvendorable and unauditable, offline needs many switches (models.dev has no off switch), and `-z` has no JSON event stream.
10. Hermes as ideas: **high value**. Reasoning replay, frozen memory snapshot, grounding rules, epistemic compaction, verification stop gate, sub-agent budgets.
