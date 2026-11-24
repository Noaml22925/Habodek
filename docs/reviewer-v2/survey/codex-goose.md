# Survey: Codex CLI and Goose

Both repos were cloned with `--filter=blob:none` into `survey/repos/{codex,goose}`. I read them and did not build or run them. Paths below are relative to the repo root. For Codex, most paths are under `codex-rs/` (written as `codex-rs/...` the first time, then shortened). For Goose, most are under `crates/`.

---

## 1. Codex CLI (openai/codex)

### Identity
- URL: https://github.com/openai/codex
- Commit read: `d916a952a73673f7f9c0c890606ab54f1bdb79a1`, dated 2026-11-23.
- 4,271 commits in the last 90 days. 385 distinct author emails in the last 180 days.
- Latest stable tag: `rust-v0.171.0` (2026-11-19). Newest tag: `rust-v0.173.0-alpha.4`.
- The project moves extremely fast: roughly 47 commits a day.

### Licence
- Apache-2.0 (`LICENSE`). Every crate uses `license = "Apache-2.0"` (`codex-rs/Cargo.toml:187`).
- `NOTICE` credits code derived from Ratatui (MIT).
- Contributions require a CLA (`docs/CLA.md`, `.github/workflows/cla.yml`). This does not restrict use.
- No closed components are needed for exec. Several features only work against OpenAI services (ChatGPT login, cloud tasks, hosted web search, remote compaction).

### Runtime and dependencies
- Rust workspace of 139 crates. `Cargo.lock` has 1,503 packages, 14 of them from git: forks of crossterm, tungstenite, sqlx, rusty_v8, plus rmcp, nucleo and mxc-sdk (`Cargo.toml:452-701`).
- `cargo vendor` should handle all of them (not verified by building).
- V8 (`rusty_v8`, whose build normally downloads a prebuilt archive) is only used by `code-mode-host` and `code-mode-runtime`. Per the Cargo.toml files, neither `exec`, `cli` nor `app-server` depends on them; `app-server` only uses the gRPC client crate `codex-code-mode`. Not verified with `cargo tree`.
- Ships as a single static binary (musl targets in `codex-cli/scripts/build_npm_package.py:27-34`). The npm wrapper just locates the vendored binary (`codex-cli/bin/codex.js:81-90`).
- The sandbox prefers the system `bwrap` and falls back to a bundled one (`linux-sandbox/README.md`).

### Outbound calls / telemetry (exec path)

| # | Call | Where | Default | Off switch |
|---|---|---|---|---|
| 1 | Statsig OTLP metrics to `https://ab.chatgpt.com/otlp/v1/metrics`, with an embedded API key | `otel/src/config.rs:11-38` | **ON** in release builds. `metrics_exporter` defaults to Statsig (`config/src/types.rs:682-683,724`) and exec sets `DEFAULT_ANALYTICS_ENABLED=true` (`exec/src/lib.rs:190,738-744`) | `-c analytics.enabled=false` (gated at `core/src/otel_init.rs:75-83`) or `-c otel.metrics_exporter="none"` |
| 2 | Analytics events to `<chatgpt_backend>/codex/analytics-events/events` | `analytics/src/client.rs:183-187` | Sent only with OpenAI/ChatGPT auth (`client.rs:1124-1131`) | `analytics.enabled=false` drops the queue (`client.rs:434`) |
| 3 | Curated plugin sync from `github.com/openai/plugins.git`, `api.github.com` and the `chatgpt.com/backend-api/plugins/export/curated` fallback, plus a marketplace auto-upgrade thread | `core-plugins/src/startup_sync.rs:23-31`, `core-plugins/src/manager.rs:964-980,3160-3200` | **ON**: the in-process app-server that exec embeds starts plugin startup tasks (`app-server/src/in_process.rs:620`, `message_processor.rs:595-613`), and `plugins` is Stable/default-on (`features/src/lib.rs:1988-1991`) | `--disable plugins` or `-c features.plugins=false` (`cli/src/main.rs:969-974`) |
| 4 | Model catalogue fetch | `models-manager/src/manager.rs:991-996` | Only for the OpenAI backend, a gateway OAuth provider, command auth or a `model_catalog_url`. A custom provider with `env_key` does not fetch | You can also pin a static catalogue with `model_catalog_json` (`config/src/config_toml.rs:422`, used at `model-provider/src/provider.rs:730-736`) |
| 5 | Update check against `api.github.com/repos/openai/codex/releases/latest` | `tui/src/updates.rs:62` | TUI only; not in exec | n/a |
| 6 | Sentry feedback upload | `feedback/src/lib.rs:63` | Only on explicit `/feedback` | `feedback.enabled=false` (`config/src/types.rs:257-261`) |
| 7 | Managed cloud-config bundle | `exec/src/lib.rs:543-550` | Appears to need ChatGPT bootstrap identity | Not verified end to end |
| 8 | Hosted `web_search` tool declared to the provider. Codex makes no outbound call itself, but the provider capability defaults to true (`model-provider/src/capabilities.rs:26`, `core/src/tools/spec_plan.rs:693-721`) | | | `-c web_search="disabled"` (`config_toml.rs:495`) |
| 9 | `--oss` mode pulls models through local Ollama (`ollama/src/client.rs:185-189`) or `lms get` (`lmstudio/src/client.rs:178`) | | Only with `--oss` | |

OTel logs and traces default to `None` (`config/src/types.rs:722-723`).

### LLM layer: Chat Completions support is gone
This is the decisive finding:
- `WireApi` has a single variant, `Responses` (`model-provider-info/src/lib.rs:116-123`).
- `wire_api = "chat"` is a hard deserialisation error: "`wire_api = "chat"` is no longer supported" (`lib.rs:112,134-146`).
- It was removed by commit `d2394a2494` on 2026-02-03, "chore: nuke chat/completions API (#10157)".
- The `ollama-chat` provider was removed as well (`lib.rs:113-114`).
- The built-in `oss`, `ollama` and `lmstudio` providers are Responses-API providers on `http://localhost:{11434|1234}/v1`, overridable with `CODEX_OSS_BASE_URL` or `CODEX_OSS_PORT` (`lib.rs:777-787,900-917`).

What a custom provider can set (`lib.rs:150-245`): `base_url`, `env_key`, `http_headers`, `env_http_headers`, `query_params`, `request_max_retries` (default 4), `stream_max_retries` (default 5) and `stream_idle_timeout_ms` (default 300 s) (`lib.rs:73-75`).

Requests are stateless: `store:false` and the full input every turn (`codex-api/src/common.rs:408-436`). Codex always asks for `include:["reasoning.encrypted_content"]` (`core/src/client.rs:1334`).

How reasoning is handled:
- Reasoning arrives as Responses `reasoning` items {summary, content, encrypted_content} (`protocol/src/models.rs:1187-1200`).
- Raw `reasoning_text` deltas are parsed (`codex-api/src/sse/responses.rs:452-458`).
- When history is replayed, `content` is serialised only if it contains `ReasoningText` (`models.rs:1902-1908`). So raw chain of thought from open-weight models is echoed back as a reasoning input item.

The consequence for us: Codex can only talk to the Qwen and GLM models through **LiteLLM's `/v1/responses` bridge**. Whether LiteLLM maps Responses `reasoning` input items back into `reasoning_content` on chat-completions assistant messages across multi-turn tool use is **not verified**, because it lives outside this repo. That is the single biggest risk.

`--output-schema` is sent as Responses `text.format` with a strict `json_schema` (`codex-api/src/common.rs:535-553`, `core/src/session/turn.rs:2081-2082`). The server must enforce it. There is no client-side jsonschema validation in `core` or `exec` (grep found none).

There is no Qwen, GLM, Zhipu or LiteLLM-specific code. "litellm" appears only in tests (`rmcp-client/src/http_headers_tests.rs:324`).

An unknown model slug gets fallback metadata (`models-manager/src/model_info.rs:98-150`):
- 272k context window. Override with `model_context_window`.
- 10 kB tool-output truncation. Override with `tool_output_token_limit` (`model_info.rs:19-44`).
- `apply_patch_tool_type: None`, so the apply_patch tool is not registered. A reviewer doesn't need it.
- The local base prompt `models-manager/prompt.md`.

### Headless mode
Command: `codex exec [OPTIONS] [PROMPT]`. If the prompt is `-` or omitted, it is read from stdin; piped stdin is appended as a `<stdin>` block (`exec/src/cli.rs:104-108`).

| Flag | What it does | Where |
|---|---|---|
| `--json` | JSONL events | `cli.rs:86-93` |
| `--output-schema FILE` | JSON schema for the final message | `cli.rs:75-77` |
| `-o FILE` | Write the last message to a file | `cli.rs:95-102` |
| `--ephemeral` | Don't persist the session | `cli.rs:63-65` |
| `--ignore-user-config` | Skip the user config | `cli.rs:67-69` |
| `--skip-git-repo-check` | Run outside a git repo | `cli.rs:54-56` |
| `-m`, `--oss` / `--local-provider` | Model and local-provider selection | `utils/cli/src/shared_options.rs:21-32` |
| `-s read-only\|workspace-write\|danger-full-access` | Sandbox mode | `shared_options.rs:38-41` |
| `-c key=value` | Config overrides | |
| `--enable` / `--disable FEATURE` | Feature toggles | |

Subcommands are `resume`, `fork` and `review --base BR | --commit SHA | --uncommitted | PROMPT` (`cli.rs:195-205,319-352`).

- Approvals: headless defaults to `AskForApproval::Never` (`exec/src/lib.rs:651-653`).
- Exit codes: 1 outside a git repo (`lib.rs:1100-1106`) and 1 if any error was seen (`lib.rs:1608-1611`). Otherwise 0.
- JSONL schema (`exec/src/exec_events.rs`):
  - Events: `thread.started{thread_id}`, `turn.started`, `turn.completed{usage{input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens}}`, `turn.failed{error}`, `item.started|updated|completed{item}` and `error{message}` (`:8-94`).
  - Item types: `agent_message{text}` (a JSON string when a schema is used), `reasoning{text}`, `command_execution{command, aggregated_output, exit_code, status}`, `file_change`, `mcp_tool_call`, `collab_tool_call`, `web_search`, `todo_list` and `error` (`:107-133`).
  - TypeScript types are generated with ts-rs, and the TS SDK spawns `exec --experimental-json` (`sdk/typescript/src/exec.ts:95`).

### Agent loop
- `run_turn` (`core/src/session/turn.rs:227`) loops at `:573`. Each sampling request streams with up to `stream_max_retries` retries (`:2131-2145`). After each response, `needs_follow_up = model_needs_follow_up || has_pending_input` (`:757`).
- It stops when there is no follow-up and Stop hooks allow it (`:872-930`). A Stop hook can **block** and inject a continuation prompt (`:894-915`). That is a natural place to plug in a "did you verify?" check.
- There is no default max-steps cap (none found).
- An unknown tool or bad arguments becomes `FunctionCallError::RespondToModel`, which is returned to the model as tool output (`tools/src/function_call_error.rs:7`, `core/src/tools/registry.rs:1296`).

### Context management / compaction
- **When:** auto-compaction triggers at 90% of the context window, or at `model_auto_compact_token_limit` if that is lower (`protocol/src/openai_models.rs:574-585`). It runs pre-turn, mid-turn or post-turn (`turn.rs:760-772,1810-1870`).
- **Which kind:** remote compaction (the `/responses/compact` endpoint) is used only for OpenAI and Azure (`model-provider/src/capabilities.rs:33-55`). Custom providers get local summarisation: `SUMMARIZATION_PROMPT`, a short "handoff summary" prompt (`prompts/templates/compact/prompt.md`).
- **What it keeps:** the new history is the summary prefixed with `summary_prefix.md`, plus the most recent user messages up to a 20k-token budget (`core/src/compact.rs:69,733-800`). Initial context is re-injected (`compact.rs:90-121`).
- **Overflow:** if the compaction request itself overflows, the oldest item is dropped and the request retried (`compact.rs:359-370`).
- **Tool output truncation:** middle-truncated ("…N tokens truncated…") by `utils/output-truncation/src/lib.rs:54`. `exec_command` accepts a `max_output_tokens` argument, default 10,000 (`core/src/unified_exec/mod.rs:74-76`).

### Tools
- `exec_command` with `cmd`, `workdir`, `yield_time_ms` and `max_output_tokens`, plus `write_stdin` for PTY sessions (`spec_plan.rs:1194-1231`, `handlers/shell_spec.rs:17-39`).
- `apply_patch`, only if the model metadata declares it (`spec_plan.rs:1391-1393`).
- `update_plan`, `view_image`, MCP tools and resources (`:1234-1255`), collab tools, and hosted `web_search`.
- There are **no** read-file, grep, glob, LSP or repo-map tools. Code is read through the shell (`rg`, `sed`, `cat`).

### Sub-agents
- The `multi_agent` feature (Collab) is default-on (`features/src/lib.rs:1742-1746`).
- Tools: `spawn_agent{message|items, agent_type, fork_context, model, reasoning_effort}` (`core/src/tools/handlers/multi_agents_spec.rs:625-656`), `send_input`, `wait` and `close_agent` (`exec_events.rs:220-228`).
- A child is a separate thread that starts with only its prompt, unless `fork_context` is set. Depth is limited by `agent_max_depth` (`spec_plan.rs:735-748`).

### Memory
- `AGENTS.md` and `AGENTS.override.md` are concatenated from the git root down to the cwd (`core/src/agents_md.rs:1-50`), capped at 32 KiB (`core/src/config/mod.rs:280`).
- A "memories" extraction feature exists but is off by default (`features/src/lib.rs:1536-1540`).

### Sandboxing
- Linux uses bubblewrap namespaces plus a seccomp filter; the older Landlock code is retained (`linux-sandbox/README.md:9-14`).
- The seccomp network filter allows only `AF_UNIX` sockets (`linux-sandbox/src/seccomp.rs:186-351`).
- `--sandbox read-only` exists (`utils/cli/src/sandbox_mode_cli_arg.rs:13-17`). Read-only plus `Never` approvals is a good shape for a reviewer.

### Code review support: the `/review` feature (directly relevant)
- **Prompts.**
  - Target prompts (`prompts/src/review_request.rs:18-99`). For example, the base-branch prompt computes the merge-base itself and tells the model to run `git diff <sha>` (`:21,62-77`).
  - The rubric becomes the system prompt (`prompts/templates/review/rubric.md`). It sets the bug criteria: introduced by this commit, provably affects other code, not speculative (`:10-19`). It sets comment style (`:21-31`), "list every qualifying finding" (`:34-36`), P0-P3 tags (`:56-58`), rule attribution to AGENTS.md (`:46-52`) and a strict JSON schema: findings with title, body, confidence_score, priority and code_location{absolute_file_path, line_range}, plus overall_correctness (`:67-95`).
- **Mechanism.**
  - `spawn_review_thread` disables web search and goals (`core/src/session/review.rs:30-35`) and seeds the user prompt (`:143-174`).
  - `ReviewTask` runs a **separate one-shot sub-agent** with base instructions set to the rubric, approvals Never and collab tools disabled (`core/src/tasks/review.rs:104-141`).
  - It passes `final_output_json_schema: None` (`:136`), so the JSON depends on the prompt alone.
  - It parses the last agent message leniently: strict JSON first, then the first `{…}` substring, then a plain-text fallback (`:192-207`).
  - Only the result goes back into the parent thread history (`:211-285`).
- **Limitation for us:** in `exec --json` the structured findings are rendered to text (`protocol/src/review_format.rs:64-82`). There is no review item type in the JSONL schema. To get machine-readable findings, run a plain `codex exec` with the rubric and `--output-schema`.

### Fit for Habodek
- **(1) As the engine: conditional, high risk.**
  - The loop, sandbox, JSONL and review flow are excellent.
  - **Biggest blocker:** Codex is Responses-API only. Everything depends on LiteLLM's Responses-to-Chat bridge correctly round-tripping function calls and `reasoning_content` for Qwen and GLM, and honouring strict `text.format`. None of that is verified.
  - Telemetry (Statsig metrics) and plugin sync are on by default, but config switches turn them off.
  - The churn (≈47 commits/day) makes a vendored fork costly.
- **(2) As a source of ideas: very high.**

### Ideas worth stealing
1. The review rubric (`prompts/templates/review/rubric.md:10-95`): "provably affected", "introduced by this change", confidence and priority per finding, short line ranges that overlap the diff, and AGENTS.md rule citations.
2. Running the review in an isolated sub-thread whose only output is the parsed findings (`core/src/tasks/review.rs:98-142,211-285`).
3. Lenient JSON extraction with a text fallback (`tasks/review.rs:192-207`).
4. Stop hooks that can block finishing and inject a continuation (`turn.rs:872-916`). Use one to force "verify every claim before final_output".
5. Middle truncation with an "original token count" header, plus a per-call `max_output_tokens` argument (`utils/output-truncation/src/lib.rs:54`, `handlers/shell_spec.rs:39`).
6. When compaction overflows, drop the oldest item and retry (`compact.rs:359-370`).

---

## 2. Goose (block/goose, now badged as aaif-goose/goose)

### Identity
- URL: https://github.com/block/goose. The README badges point to `aaif-goose/goose`.
- Commit read: `059fee25f13fa92be561a7d42cdd04ececef5a98`, dated 2026-11-23.
- 588 commits in the last 90 days. 265 distinct author emails in the last 180 days.
- Latest release: `v1.60.2`. The internal `oi-kit` crates are tagged `oi-kit-v0.1.0-alpha.15`.

### Licence
- Apache-2.0 (`LICENSE`, `Cargo.toml:12`).
- No CLA or DCO found in `CONTRIBUTING.md`. `GOVERNANCE.md` describes a lightweight maintainer model.
- The repo includes an Electron desktop UI (`ui/`, npm). The CLI does not need it.

### Runtime and dependencies
- Rust workspace: `goose`, `goose-cli`, `goose-mcp` and the `oi-kit-*` crates.
- `Cargo.lock` has 1,103 packages with one git source (cudaforge, used only for CUDA).
- The tiktoken BPE is embedded (`crates/goose/src/token_counter.rs:216`). Tree-sitter grammars are cargo deps, not runtime downloads (`crates/goose/Cargo.toml` `tree-sitter` feature).
- Default CLI features include local-inference (candle, HF hub), AWS, live-voice, `update` and `online-model-meta` (`crates/goose-cli/Cargo.toml` `[features]`). A slim offline build would use `--no-default-features` plus selected features. Not verified that it compiles.

### Outbound calls / telemetry

| # | Call | Where | Default | Off switch |
|---|---|---|---|---|
| 1 | PostHog to `us.i.posthog.com` | `crates/goose/src/posthog.rs:16-17` | **Opt-in**: false until the user opts in (`:33-52`) | `GOOSE_TELEMETRY_OFF=1` (`:20-27`) |
| 2 | **models.dev catalogue** (`https://models.dev/api.json`, 15 s timeout, background task, failure only logged) | `crates/goose/src/model_catalog.rs:5,11-31`, `oi-kit-provider-types/src/canonical/registry.rs:149-160` | **ON**; the call is compiled in behind the `online-model-meta` cargo feature (`goose-cli/src/main.rs:24-25`), which is default-on | **No runtime switch.** Rebuild without the feature. The bundled catalogue is the fallback (`registry.rs:16-23`) |
| 3 | OSV malware check for `npx`/`uvx` stdio extensions | `oi-kit-agent/src/mcp/malware_check.rs:15-21,363` | Fails open on network error (`:423-439`) | Point `OSV_ENDPOINT` elsewhere, or don't use npx/uvx extensions |
| 4 | OTLP | `crates/goose/src/otel/otlp.rs:224-264` | Only if `OTEL_EXPORTER_OTLP_ENDPOINT` or config is set | `OTEL_SDK_DISABLED` |
| 5 | Self-update from GitHub with sigstore | `goose-cli/src/commands/update.rs:131-135` | Only on `goose update` | `disable-update` feature |
| 6 | Opt-in extras | Security prompt classifier (`crates/goose/src/security/mod.rs:47-101`), TypeSafe "context relevance shadow" (`crates/goose/src/agents/state_machine_builder.rs:71-102`), GitHub recipe search (`GOOSE_RECIPE_GITHUB_REPO`, `goose-cli/src/recipes/search_recipe.rs:23`) | Off | n/a |
| 7 | LiteLLM `/model/info` | `crates/goose/src/providers/litellm.rs:145-160` | Called on our own gateway, not external | n/a |

### LLM layer
- Native Chat Completions with function calling.
- There is a dedicated `litellm` provider:
  - Settings: `LITELLM_HOST` (default `http://localhost:4000`), `LITELLM_API_KEY`, `LITELLM_BASE_PATH` (default `v1/chat/completions`), `LITELLM_CUSTOM_HEADERS`, `LITELLM_TIMEOUT` and `LITELLM_SUPPORTS_STREAMING` (`providers/litellm.rs:27-77`).
  - It discovers context limits and vision support from LiteLLM `/model/info` (`:145-160,312-318`).
- Declarative OpenAI-engine custom providers also work, with `preserves_thinking` defaulting to true for the openai engine (`oi-kit-providers/src/declarative.rs:215-217`).
- **Reasoning handling, the best of all surveyed so far.** The LiteLLM provider calls `create_request` with `preserve_thinking_context: true` (`oi-kit-provider-types/src/formats/openai.rs:1725-1745,litellm.rs:321`). The formatter then:
  - Propagates the turn's `reasoning_content` onto **every** assistant tool-call message, and clears it at a new real user message (`formats/openai.rs:249-283`). This is what DeepSeek, Kimi, Qwen and GLM interleaved thinking expects.
  - Omits empty `reasoning_content` (`:539-542`).
  - Can inline reasoning as `<think>…</think>` for models that reject the field, via `ThinkingPreservationFormat::{ReasoningContent, ContentPrepend, ContentXml}` (`:560-590`, `base.rs:239-246`).
  - Merges split tool-call messages (`:597-651`).
  - Parses `reasoning_content`, `reasoning` or inline `<think>` blocks from responses (`:800-820`), including when streaming (`:1305-1313`).
- Qwen and GLM are covered by first-class declarative definitions: `zhipu.json` (GLM-4.5 to 5.3 with context limits, `preserves_thinking: true`), `alibaba.json:52` and `zai.json`. `zai.json` uses the anthropic engine and emits GLM `clear_thinking` (`oi-kit-providers/src/anthropic.rs:486-627`).
- **Toolshim:** for models without tool calling, the text output goes to a second "interpreter" model that extracts tool calls. The default interpreter is Ollama `mistral-nemo` (`GOOSE_TOOLSHIM_BACKEND`, `GOOSE_TOOLSHIM_MODEL`; `crates/goose/src/providers/toolshim.rs:1-31,55-57`). We don't need it for Qwen or GLM.

### Headless mode
Command: `goose run`. Flags (`crates/goose-cli/src/cli.rs`):

| Flag | What it does | Line |
|---|---|---|
| `--text` / `-i FILE` (`-` for stdin) | The prompt | `:238` |
| `--recipe FILE` with `--params k=v` | Run a recipe | `:250` |
| `--system` | Extra system prompt | `:261` |
| `--sub-recipe` | Add sub-recipes | `:281` |
| `--no-profile --with-builtin developer` | Restrict extensions | `:206,215` |
| `--with-extension 'cmd…'` | Add a stdio extension | `:187` |
| `--max-turns` | Turn budget, default 1000 (`crates/goose/src/config/base.rs:1373-1375`) | `:135` |
| `--max-tool-repetitions` | Guard against repeated identical calls | `:127` |
| `--output-format text\|json\|stream-json` | Output format | `:317-321` |
| `--quiet` | Response only | `:310` |
| `--no-session` | Don't persist the session | `:370` |
| `--provider`, `--model` | Model selection | `:340,349` |
| `--container ID` | Run extensions inside a Docker container | `:143` |

- A per-extension tool allowlist is available as `available_tools` (`crates/goose/src/agents/extension_config.rs:69`).
- Approval mode `GOOSE_MODE` defaults to `Auto`, so no prompts (`oi-kit-provider-types/src/goose_mode.rs:22-34`).
- **Output** (`goose-cli/src/session/mod.rs:128-177`):
  - `json` prints `{messages[], metadata{total_tokens, input_tokens, output_tokens, cache_*, cost_usd, status}}` at the end.
  - `stream-json` prints JSONL `{"type":"message"|"notification"|"error"|"complete", …}`. Messages carry Text, ToolRequest, ToolResponse and Thinking content.
  - A terminal error yields an `error` event and a non-zero exit (`:1595-1611`). The exact exit code is not verified.
- **Structured output:** a recipe `response.json_schema` adds a `final_output` tool.
  - Arguments are **validated locally with `jsonschema`** and validation errors are fed back to the model (`oi-kit-agent/src/final_output.rs:22-57,109-137,160`).
  - If the model stops without calling it, it is told "You MUST call the `final_output` tool NOW" (`:24-25`).
  - `final_output` is rejected if called alongside other tools (`:26-28`).
- **Recipe retry:** `retry{max_retries, checks:[shell], on_failure, timeout_seconds}` re-runs until shell success checks pass (`crates/goose/src/agents/types.rs:11-24`, `crates/goose/src/recipe/mod.rs:44-87`).
- An ACP server binary also exists (`crates/goose/src/bin/goose-acp.rs`). Not examined.

### Agent loop
The loop is a state machine of operations followed by inference (`state_machine_builder.rs:141-260`). In order: compaction, final output, tool approval, skills, recipe, extensions and tool deferral, unhandled tool calls, sub-agents (`GOOSE_MAX_CONCURRENT_SUBAGENTS`, default 5, `:205-210`), steer, tool-pair compaction, retry, empty-response, thinking recovery, stop hook, exit-on-error, max turns.

Recovery details:
- Empty responses are retried 3 times (`oi-kit-agent/src/empty_response.rs:13-15`).
- **Thinking-only turns** (reasoning but no answer) get "You did some thinking but did not produce an answer. Please continue.", up to 3 times (`crates/goose/src/agents/state_machine/ops_thinking_recovery.rs:18-22`). This matters for Qwen and GLM.
- Provider retries default to 3 (`oi-kit-provider-types/src/retry.rs:14`).
- Malformed or truncated tool arguments become explicit errors returned to the model (`formats/openai.rs:61,900`).

### Context management
- **When:** auto-compaction at 80% of the context limit (`GOOSE_AUTO_COMPACT_THRESHOLD`; `oi-kit-context-management/src/lib.rs:34`), capped at 225k tokens (`GOOSE_AUTO_COMPACT_TOKEN_LIMIT`; `crates/goose/src/context_mgmt/mod.rs:27-57`).
- **How:** "in-band" (the summary is requested on the live prefix) or "standalone" (a separate request, also used to recover from context-length errors, at most 2 times) (`oi-kit-agent/src/compaction/mod.rs:1-8,45,560`).
- **What it keeps:** the summary prompt asks for a **structured JSON** with user_intent, technical_concepts, files[{path, summary, key_code}], errors_and_fixes (errors quoted verbatim), problem_solving (chosen and rejected options), pending_tasks, current_work and next_step (`oi-kit-context-management/src/prompts/compaction.md`).
- **Old tool pairs:** optional summarisation of tool-call/response pairs past a cutoff (`GOOSE_TOOL_PAIR_SUMMARIZATION`, default false; `context_mgmt/mod.rs:59-63`, `ops_tool_pair_compaction.rs:21`).
- **Tool output:**
  - Shell output is limited to 2,000 lines or 50 KB per stream. Beyond that the model gets a 50-line / 10 KB preview and the full output goes to a temp file it can read (`developer/shell.rs:161-166`).
  - Any tool result over 200k chars is redirected to a file (`crates/goose/src/agents/large_response_handler.rs:5-14`).

### Tools
- The built-in `developer` extension provides `write`, `edit` (exact unique find/replace), `shell` (structured stdout/stderr), `tree` (gitignore-aware, with line counts) and `read_image` (`crates/goose/src/agents/platform_extensions/developer/mod.rs:109-190`).
- There is **no read-with-line-numbers or grep tool**; it uses the shell.
- `analyze` is a **tree-sitter call-graph tool** with three modes: directory overview, file details, and symbol focus with `follow_depth` call-graph traversal across a directory (`platform_extensions/analyze/mod.rs:29-43,215-218`; about 2.3k LOC).
- Other built-ins: `todo`, `summon`, `chatrecall`, `code_execution` and more. Any MCP server (stdio or streamable HTTP) can be added as an extension.

### Sub-agents
- The `summon` extension provides `delegate`: ad-hoc instructions, a sub-recipe or agent as the source, or resuming an earlier delegate. Each delegate runs "independently with its own context", and its result comes back when it finishes (`platform_extensions/summon.rs:530-545`).
- A per-delegate `max_turns` overrides `GOOSE_SUBAGENT_MAX_TURNS` (`:510-513`).
- `load` pulls recipe or skill content into context (`:463-470`).

### Memory
- `.goosehints`, `AGENTS.md` and the configurable `CONTEXT_FILE_NAMES` (`crates/goose/src/hints/load_hints.rs:10-17`) are loaded, plus subdirectory hints (`SubdirectoryHintOperation`).

### Sandboxing
- **None at the OS level.** The macOS seatbelt sandbox "was experimental and has been removed" (`documentation/blog/2026-02-23-goose-v1-25-0/index.md:22`).
- The only controls are `GOOSE_MODE` approval levels and `--container`, which runs extensions in a given Docker container (`cli.rs:143`).
- We would have to wrap it in bwrap, Docker or Podman ourselves, with no network.

### Code review support
- There is no built-in review mode and no GitLab or GitHub integration in the core.
- Only community example recipes exist (`documentation/src/pages/recipes/data/recipes/*-pr-code-review.yaml`, `analyze-pr.yaml`).

### Fit for Habodek
- **(1) As the engine: the better of these two.**
  - It speaks chat-completions natively with a LiteLLM provider.
  - Its `reasoning_content` handling is correct across tool turns, and it recovers from thinking-only turns.
  - `final_output` validates the JSON schema locally.
  - It has `stream-json`, recipes and sub-agents.
  - **Biggest blockers:**
    - The models.dev fetch is compiled in by default; it needs a custom build without `online-model-meta`. It fails open, but it is still an outbound call.
    - There is no sandbox at all; we must supply one.
    - There is no review prompt; we write our own, possibly borrowing Codex's rubric.
- **(2) As a source of ideas: high.** See the list below.

### Ideas worth stealing
1. Turn-scoped `reasoning_content` replay onto every assistant tool-call message, with a `<think>` inline fallback (`formats/openai.rs:249-283,560-590`). This is directly what our Qwen and GLM loop needs.
2. A `final_output` tool with local jsonschema validation, a "call it alone" rule and a forced-continuation nudge (`oi-kit-agent/src/final_output.rs:22-57,160`).
3. Recovery steps for thinking-only and empty responses (`ops_thinking_recovery.rs:18-22`, `empty_response.rs:13-15`).
4. Shell output spill-to-file: a preview plus a path the model can read (`developer/shell.rs:161-166`).
5. A structured JSON compaction summary with files, verbatim errors and rejected options (`oi-kit-context-management/src/prompts/compaction.md`).
6. Tree-sitter `analyze` symbol-focus call graphs for "follow the callers" review behaviour (`analyze/mod.rs:29-43,215`).
7. Recipe `retry.checks`: shell success checks that force a re-run (`agents/types.rs:11-24`).

---

## Verdict summary
1. **Codex has dropped Chat Completions.** It only speaks the Responses API (`wire_api="chat"` is a hard error since 2026-02-03), so Qwen and GLM can only be reached through LiteLLM's `/v1/responses` bridge, which is not verified.
2. Codex has a strong loop, a bwrap and seccomp read-only sandbox, a clean JSONL schema and the best review rubric and flow seen so far. As an engine it is risky because of the Responses-only API, telemetry that is on by default (switchable) and very high churn.
3. Codex is off by config: `analytics.enabled=false`, `otel.metrics_exporter="none"`, `features.plugins=false` and `web_search="disabled"`.
4. Steal from Codex: `prompts/templates/review/rubric.md`, the isolated review sub-thread, lenient JSON parsing and blocking Stop hooks.
5. **Goose is the better embeddable engine of the pair.** It speaks chat-completions natively, has a LiteLLM provider and correctly replays `reasoning_content` across tool turns.
6. Goose's headless mode is `goose run --recipe … --output-format stream-json --no-session`, with structured output through a `final_output` tool that is validated locally with jsonschema.
7. Goose's blockers: the models.dev fetch is compiled in by default and needs a rebuild without `online-model-meta`, there is no OS sandbox (we bring our own), and there is no review mode.
8. Goose telemetry is opt-in (PostHog) and off with `GOOSE_TELEMETRY_OFF=1`. OTel only runs if configured, and the OSV check fails open.
9. Steal from Goose: reasoning replay, recovery from thinking-only turns, shell spill-to-file, the structured compaction JSON and the tree-sitter call-graph tool.
10. Neither has a read-with-line-numbers or grep tool, LSP, or GitLab integration. Both rely on the shell, so Habodek still owns GitLab, threads and verification.
