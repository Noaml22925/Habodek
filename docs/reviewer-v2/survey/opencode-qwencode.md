# Survey: opencode and Qwen Code

Clones: `survey/repos/opencode`, `survey/repos/qwen-code` (both `--filter=blob:none`). Paths below are relative to each repo root. I read the code and ran nothing from either repo.

---

## 1. opencode

### Identity
- URL: https://github.com/sst/opencode. It redirects; `packages/cli/package.json` names `github.com/anomalyco/opencode`.
- Commit read: `e1fb08afc547cd838ebc8ee7741d41d92edc0d6b` on branch `dev`, dated 2026-11-23.
- Activity: 5,445 commits in the last 90 days and 227 distinct author emails in the last 180 days. Both counts include merges.
- Latest tag: `v2.4.1`, 2026-11-23.
- **Major caveat:** this is "OpenCode V2", a rewrite. The old `packages/opencode` tree (which held LSP, the session loop and the rest) was deleted in commit `44b6938b2a` on 2026-07-20. Most third-party write-ups about opencode describe v1 and are now stale.

### Licence
- Root `LICENSE` is MIT, and all 24 `packages/*/package.json` files declare MIT.
- I found no CLA wording in `CONTRIBUTING.md`.
- There are no non-OSS parts in the runtime. "OpenCode Zen/Go/Console" are optional hosted services that the client can talk to.

### Runtime and vendoring
- TypeScript on Bun 1.4.4, with `bun.lock` and the Effect 4 framework throughout.
- Releases are a single Bun-compiled executable (`packages/cli/script/build.ts:185-207`). It carries per-platform native addons: node-pty, @parcel/watcher, fff, ffi-rs and OpenTUI (`packages/cli/package.json:54-80`).
- The models catalogue is bundled into the binary: `packages/core/src/models-dev/snapshot.txt`, imported at `packages/core/src/models-dev.ts:19`.
- Vendoring options:
  - Take the MIT release binary as is.
  - Build from source behind an npm/bun registry mirror.
- ripgrep is **not** bundled (see outbound calls below).

### Outbound calls and telemetry
| # | What | Where | Off switch |
|---|---|---|---|
| 1 | Model catalogue from `https://models.opencode.ai`. Fetched at boot when there is no cache, then every 5 minutes. | `core/src/models-dev.ts:648,701-706,784-805,848-850` | `OPENCODE_DISABLE_MODELS_FETCH=1` (`cli/src/server-process.ts:120`). The bundled snapshot remains the floor (`models-dev.ts:797-800`). `OPENCODE_MODELS_PATH` or `OPENCODE_MODELS_URL` select a local file or a mirror. |
| 2 | Self-update check against opencode.ai and brew. Triggered from the TUI default handler (`cli/src/commands/handlers/default.ts:150`), not from `run`. | `cli/src/services/updater.ts:323-324` | `OPENCODE_DISABLE_AUTOUPDATE=1` (`:608`) or config `autoupdate:false` (`:138-141`) |
| 3 | **ripgrep auto-download** from GitHub releases (hash-pinned). Happens when `rg` is not on PATH and not in the global bin dir. | `core/src/ripgrep/binary.ts:121-145` | No flag. Put `rg` on PATH. |
| 4 | Runtime `npm add` for three cases. **`@ai-sdk/openai-compatible` is mapped to a built-in native provider, so LiteLLM needs no install** (`core/src/aisdk-native.ts:127`). | | |
| | (a) Provider packages that are not native | `core/src/provider.ts:220`, `plugin/provider/sdk-factory.ts:8` | Configure no non-native provider |
| | (b) Plugins, plus a 24-hour update check for range-versioned plugins | `plugin/module.ts:113`, `plugin/update.ts:10` | Configure no plugins |
| | (c) Formatters, auto-installed through `Npm.which` | `util/src/npm.ts:519`, `formatter/builtins.ts:82` | Formatters are opt-in (`config/plugin/formatter.ts:30`) |
| 5 | OTLP logs and traces | `util/src/observability/otlp.ts:63-75` | Only active when `OTEL_EXPORTER_OTLP_ENDPOINT` is set |
| 6 | OpenCode Console config and skills | `plugin/provider/opencode.ts:227-231` | Only when signed in to the opencode integration |
| 7 | Web search (Exa MCP `mcp.exa.ai`) and webfetch. Model-invoked only. | `plugin/websearch/exa.ts:9`, `tool/plugin/webfetch.ts` | Permission deny rules |
| 8 | TUI tree-sitter grammars downloaded from GitHub | `tui/src/parsers-config.ts:9-146` | TUI only. Whether the `run` default (non-JSON) renderer touches it is not verified. |

- Session sharing is gone in v2 (`tui/src/config/keybind.ts:126`: "session sharing was removed").
- I found no analytics or crash reporter. grepping for posthog, sentry and telemetry only matched an MCP Code Mode default.

### LLM layer
- opencode now ships its own LLM library, `@opencode/ai` (`packages/ai`), in place of the Vercel AI SDK. Config is still written as AI SDK package names; they are rewritten to native protocols (`core/src/aisdk-native.ts:113-185`).
- Custom provider config (`schema/src/config/provider.ts:113-122`) accepts:
  - `package: "@ai-sdk/openai-compatible"` plus `settings` (baseURL),
  - `headers` and `body` overlays,
  - per-model `limit.context` and `capabilities.reasoning`,
  - a `compatibility` block.
- Tool calling is native (OpenAI `tools`).
- **Reasoning across tool turns is handled correctly:**
  - Streaming reads the `reasoning_content`, `reasoning` or `reasoning_text` delta field (`ai/src/protocols/openai-chat.ts:1018-1029`) and records which field was seen.
  - On replay, every earlier assistant message from the same model gets its reasoning back in that same field (`openai-chat.ts:560-593`; `core/src/session/runner/to-llm-message.ts:181-200`).
  - After a model switch, reasoning is downgraded to plain readable text.
  - `compatibility.reasoningField` and `requireReasoning` force the field, or force an empty field on every assistant turn (`schema/src/model-compatibility.ts:25-30`).
- Qwen- and GLM-specific code:
  - `alibaba-chat` protocol: `enable_thinking`, `preserve_thinking`, `clear_thinking`, `thinking_budget` (`ai/src/protocols/alibaba-chat.ts:11-80`).
  - `zai-chat` protocol: `thinking.clear_thinking`, and `tool_stream` auto-on for GLM 4.6 and later (`zai-chat.ts:10-61`).
  - GLM and Qwen thinking toggles in `core/src/variant.ts:184-189`.
  - Qwen cache policy in `ai/src/cache-policy.ts:84-89`.
- **Caveat:** these protocols are chosen by *providerID* (`aisdk-native.ts:142-185`). A custom `litellm` provider gets the generic openai-compatible protocol, so Qwen/GLM thinking knobs must go through a `body` overlay (for example `chat_template_kwargs`).
- LiteLLM is acknowledged directly:
  - its context-window-exceeded error text is matched (`ai/src/provider-error.ts:43-44`),
  - an empty `tools: []` is sent for LiteLLM-Anthropic (`openai-chat.ts:754`).

### Headless mode
- **`opencode run [message..]`** (`cli/src/commands/commands.ts:419-468`). Flags:
  - `--format json`
  - `-m provider/model#variant`
  - `--agent`
  - `-s` / `-c` / `--fork`
  - `-f` to attach a file
  - `--thinking`, which emits reasoning events
  - `--archived`
  - `--auto`, which auto-approves anything not explicitly denied (`:49-52`)
- Output is JSONL: `{type,timestamp,sessionID,part}`. Event types are `text`, `reasoning`, `tool_use` (with state, input, output and error), `step_start`, `step_finish`, `error`, `background_wait` and `retry` (`cli/src/run/noninteractive.ts:368-492`).
- Exit code is 1 on a final error (`:306`).
- Without `--auto`, each permission request is rejected with feedback the model can see (`:97-110`). `question` forms are cancelled.
- `run` reuses the background service if one is running; otherwise it starts a private server (`commands/handlers/run.ts:10`).
- **`opencode serve`** (`commands.ts:613-627`) exposes the v2 HttpApi plus an SSE event feed (`server/src/event-feed.ts:29-31`), protected by a password (`OPENCODE_SERVER_PASSWORD`). Typed clients live in `packages/client` and `packages/sdk`. `opencode acp` starts an ACP server.
- Tools are restricted with ordered permission rules `{action, resource, effect}` in config (`schema/src/config.ts:62`) or per agent.

### Agent loop
Source: `core/src/session/runner/llm.ts`.
- `drain` handles inbox promotion and loops over steps (`:216-224`).
- `runStep` (`:237-387`) runs this sequence on each step:
  1. Auto-compaction check (`:253-259`).
  2. Background summary precompute (`:260`).
  3. Agent `steps` limit. When hit, `MAX_STEPS_PROMPT` is appended and `toolChoice:"none"` is set while tool definitions stay in place to keep the prompt cache (`:261-281`, `max-steps.ts`).
- Retries go through the `ModelRetry` policy plus a plugin `session.retry` hook (`retry.ts:94-139`).
- A context-overflow error triggers one compaction and a retry (`:319-325`).
- `finish=length` gets up to two "continue" nudges, then fails (`:38`, `:346-376`).
- Tool calls left running by a crash are failed on the next start (`:419-442`).
- Malformed tool input:
  - A repair plugin coerces stringified JSON, numeric strings, nulls and similar, and aliases `bash`→`shell` (`plugin/tool-input-repair.ts:11-29`).
  - Calls that fail with tool-input errors stay in history so the model can reissue them (`to-llm-message.ts:160-170`).

### Context management
Source: `core/src/session/compaction.ts`.
- **Trigger:** keep 15% (at least 16k tokens) of the window free (`:126-132`; `calculateCeiling` `:1148-1156`).
  - With an unknown window it compacts **only** on a provider overflow error (`:1150`). So set `limit.context` for LiteLLM models.
- **Summary:**
  - Structured template with Objective, Requirements, Decisions, Work State (completed, active, blocked), Next Move, Relevant Files (at most 15) and Important Context (`:170-209`).
  - Updated in place on later compactions (`:211-242`).
  - A nudge is re-sent if the reply skips the template (`:244`).
- **Kept verbatim:** the newest `keep` tokens of conversation (default 15k, `:266`), carried as `<conversation-checkpoint><summary/><recent-context/></conversation-checkpoint>` (`:445-460`).
- **Background precompute:** a summary starts 60k tokens before the ceiling and is swapped in when compaction is due (`:155`, `:348-400`).
- **Summary request:** sent as the same prefix to reuse the prompt cache. It is shrunk by levels: tool outputs clipped to 2,000 characters, then removed, then user-only (`:141-147`). Repeated "too long" rejections shrink it further (`:124`).
- **Tool output:** capped at 2,000 lines or 50 KiB. The full output is saved to a file under the data dir and kept 7 days. A marker names the file (`core/src/tool-output.ts:13-16,95-115`). Limits are configurable through `tool_output.max_lines` and `max_bytes`.

### Tools
Source: `core/src/tool/plugin/`.
- `read`: `N: line` numbering, at most 2,000 lines, offset and limit (`read.ts:24-40,389`).
- `grep` (ripgrep, bounded), `glob`, `shell`, `edit`, `write`, `patch` (apply_patch grammar).
- `webfetch`, `websearch`, `todo`, `question`, `skill`, `subagent`.
- `session-search` (searches past sessions), `automation`, `artifact`, image/video generation, `mcp-resource`.
- Code Mode can wrap tools into a single `execute`.
- **LSP has been removed and is a TODO in v2:**
  - `tool/plugin/edit.ts:256` and `write.ts:33`: "Add LSP notification and diagnostics after LSP runtime exists".
  - `core/src/file-mutation.ts:182`.
  - Only the config schema remains (`schema/src/config/lsp.ts`).
  - Because there is no LSP, there is no LSP auto-download at HEAD. The v1 LSP auto-download and its off switch are not verified; I would check tag `v1.19.6` `packages/opencode/src/lsp/launch.ts`.
- There is no repo map, no tree-sitter in core and no blame tool; git work goes through `shell`.

### Sub-agents
- The `subagent` tool (`tool/plugin/subagent.ts:17-58`) **always runs in the background** in a child session that starts with fresh context.
- The result comes back as a `<background-result kind="subagent">` message each time the child goes idle. The parent can send follow-ups by `sessionID`.
- Depth limit is `experimental.subagent_depth`, default 1 (`session/subagent-depth.ts:14`).
- Built-in agents (`plugin/agent.ts:63-95`):
  - `general`: question and subagent denied.
  - `explore`: everything denied except `shell`, `grep`, `glob`, `read`, `webfetch` and `websearch`.
- `run` waits for background jobs and emits `background_wait`.

### Memory
- AGENTS.md is read up the tree, with CLAUDE.md as a fallback (`core/src/instruction-discovery.ts:128-135`).
- Global `AGENTS.md` and `~/.claude/CLAUDE.md` are also read (`config/plugin/instruction.ts:41-43`).
- The `home-memory` plugin keeps memory, docs and scripts folders for the user's home workspace (`plugin/home-memory.ts`). When it activates is not verified.

### Sandboxing
- None. grepping for sandbox, bwrap, landlock and seatbelt found nothing in core or cli.
- Isolation is permission rules only, plus git worktrees (`run --worktree`).

### Code review support
- `/review` command prompt (`core/src/plugin/command/review.txt`, 100 lines). It handles `git diff`, a commit, a branch or `gh pr diff`, and asks the model to read whole files, focus on bugs and avoid noise.
- No GitLab MR integration. `plugin/provider/gitlab.ts` is the GitLab Duo *model* provider, not an MR integration.

### Fit for Habodek
- **As the engine: workable, with reservations.**
  - Strengths:
    - MIT licence.
    - All outbound calls can be switched off: `OPENCODE_DISABLE_MODELS_FETCH=1`, `OPENCODE_DISABLE_AUTOUPDATE=1`, `rg` on PATH, no plugins, deny web tools.
    - Best-in-class `reasoning_content` replay.
    - Clean JSONL from `run`, or a real HTTP+SSE API from `serve`.
  - Weaknesses:
    - No LSP and no sandbox at HEAD.
    - Extreme churn: about 60 commits a day, and a full rewrite four months ago.
    - The Bun binary carries native addons.
    - Qwen/GLM protocol niceties are not applied to a custom provider ID.
- **Single biggest blocker:** stability and churn. The pinned version would have to be frozen and patched in-house. No hard constraint is violated.
- **As a source of ideas:** very good (see below).

### Ideas worth stealing
- Compaction checkpoint format and "update the checkpoint, newer wins" prompt (`compaction.ts:170-242`). Background precompute so compaction never blocks (`:348-400`). Shrink-by-levels transcript for the summariser (`:141-147`).
- Reasoning replay keyed by the *observed* stream field, dropped after a model switch (`openai-chat.ts:560-593`, `to-llm-message.ts:181-200`).
- Max-steps handling: inject a "text-only, summarise what's done" instruction and set `toolChoice:none` while keeping tool definitions for the cache (`llm.ts:261-281`).
- Truncated tool output points at a saved full-output file (`tool-output.ts:95-115`).
- Tool-input repair layer: schema-aware coercion and name aliases (`plugin/tool-input-repair.ts`).

---

## 2. Qwen Code

### Identity
- URL: https://github.com/QwenLM/qwen-code.
- Commit read: `c134a03e5adf7631069269bb96a65cbf5c90f01a` on `main`, dated 2026-11-23.
- Activity: 3,981 commits in the last 90 days and 267 distinct author emails in the last 180 days.
- Latest release: `v0.28.4`, 2026-11-22. Nightlies are published too.
- It is a fork of Gemini CLI, diverged heavily: about 109 tool modules and a desktop app, channels, a daemon and SDKs.

### Licence
- Root `LICENSE` is Apache-2.0, and package manifests say Apache-2.0.
- `packages/cua-driver/typescript` is MIT (Cua AI).
- I found no CLA in `CONTRIBUTING.md` and no source-available parts.

### Runtime and vendoring
- TypeScript on Node 22 or newer, with `pnpm-lock.yaml`. It is bundled by esbuild into `dist` (`package.json:59`).
- **Committed vendored binaries:**
  - ripgrep for 5 platforms (`packages/core/vendor/ripgrep/*/rg`, used by `core/src/utils/ripgrepUtils.ts:164`),
  - `qwen-landlock-run` (`vendor/landlock-run/{x64,arm64}-linux`),
  - tree-sitter wasm.
- `postinstall` only chmods files (`core/scripts/postinstall.js`). There are no install-time downloads.
- Optional native deps: node-pty and a clipboard module.
- Vendoring is easy: `pnpm install` against a mirror, then the bundle.

### Outbound calls and telemetry
| # | What | Where | Off switch |
|---|---|---|---|
| 1 | **Usage statistics (QwenLogger) to Aliyun RUM `gb4w8c3ygj-default-sea.rum.aliyuncs.com`. ON by default.** | `core/src/telemetry/qwen-logger/qwen-logger.ts:83,218-219,430`. Default true at `core/src/telemetry/config.ts:44-52` and `core/src/config/config.ts:3700`. | `QWEN_USAGE_STATISTICS_ENABLED=false` or `privacy.usageStatisticsEnabled:false` (`cli/src/config/settingsSchema.ts:1573`) |
| 2 | models.dev catalogue (`https://models.dev/api.json`), refreshed at most daily | `core/src/models/model-catalog.ts:41`; triggered at `config.ts:4400` | Off when usage stats are off, or `QWEN_CODE_MODELS_DEV_REFRESH=off` (`model-catalog-refresh.ts:35`), or `model.catalogRefresh:false`. `QWEN_CODE_MODELS_DEV_URL` sets a mirror. The bundled snapshot remains. |
| 3 | npm update check | `cli/src/startup/startup-prefetch.ts:157-160`. Interactive post-render only. | `general.enableAutoUpdate:false` |
| 4 | OpenTelemetry (local, GCP or OTLP) | `core/src/config/config.ts:3668`. Default `enabled:false`. | Leave it off |
| 5 | Installation-ID header `x-gemini-api-privileged-user-id` sent when usage stats are on | `core/src/core/llm-content-generator/index.ts:34-41` | Turn usage stats off. Which generators send it is not verified. |
| 6 | Fire-and-forget HEAD "preconnect" to the model base URL | `cli/src/utils/apiPreconnect.ts` | Only reaches the LiteLLM gateway |
| 7 | Extensions and marketplace (GitHub API, codeload, npm registry) | `core/src/extension/github.ts:473-561`, `marketplace.ts:261-274` | User-initiated only |
| 8 | web_search (DashScope and others) and web_fetch | `core/src/tools/web-search*.ts`, `web-fetch.ts` | `--exclude-tools` or permissions |
| 9 | Docker sandbox image `ghcr.io/qwenlm/qwen-code:<ver>` | root `package.json` `config.sandboxImageUri` | Only with `--sandbox docker/podman`. Pre-pull or mirror it. |

- Gemini CLI's Clearcut is gone: grepping for `clearcut` returns nothing.
- LSP does no downloads. It is behind `--experimental-lsp` (default false), and servers come from the user's `.lsp.json` or extensions (`core/src/lsp/LspConfigLoader.ts:42`).
- **Offline recipe:** `QWEN_USAGE_STATISTICS_ENABLED=false`, `QWEN_CODE_MODELS_DEV_REFRESH=off`, `general.enableAutoUpdate:false`, exclude web tools, no extensions. Whether anything else phones home in headless mode is not verified exhaustively. I would run it under a deny-all egress proxy and watch the log.

### LLM layer
- OpenAI-compatible endpoints are configured with `--openai-base-url` and `--openai-api-key` (top-level options), or with `modelProviders` in settings (`baseUrl`, `envKey`, `generationConfig`, `extra_body`) (`cli/src/config/settingsSchema.ts:449-470`).
- Tool calling is native, with a streaming tool-call parser (`openaiContentGenerator/streamingToolCallParser.ts`).
- **Qwen-specific code:**
  - **XML tool-call fallback** that recovers Qwen3-Coder-style `<function=…><parameter=…>` and `<invoke name=…>` tool calls that leaked into text (`core/src/core/xml-tool-call-fallback.ts:37-40,1010-1071`).
  - `<think>` tag-leak parser (`taggedThinkingParser.ts`; enabled for qwen3 at `provider/default.ts:340-347`).
  - For qwen3 models, `reasoning_content` is mirrored into `reasoning` (`provider/default.ts:57-82,246-250`).
  - DashScope provider sends `preserve_thinking:true` (`provider/dashscope.ts:486,515`), but it is **hostname-gated** (`:221-246`). Through LiteLLM, add `preserve_thinking` or `enable_thinking` via `extra_body`.
- **GLM-specific code:**
  - `ZaiOpenAICompatibleProvider` is selected for `glm-*` model names. Its wire reshape (flat `reasoning_effort`) happens only for z.ai and bigmodel.cn hosts (`provider/zai.ts`).
  - GLM-5 token limits in `core/src/core/tokenLimits.ts:318,379`.
- **Reasoning across turns:**
  - Thoughts are kept in history as `thought` parts and replayed as `reasoning_content` on each assistant message (`openaiContentGenerator/converter.ts:585-593,720-750`).
  - For strict endpoints the field is stripped (`provider/utils.ts:69-90`) or forced to empty (`:47-66`).
  - Thoughts are stripped only on idle microcompaction (`core/src/core/llm-chat.ts:3402-3405`) and in an ACP path (`cli/src/acp-integration/session/Session.ts:6005`).
- **Model-name caveat:** detection is by model name. If LiteLLM exposes aliases that are not qwen3 or glm names, the Qwen/GLM paths will not trigger.
  - Context window defaults to 200k when the name is unknown (`tokenLimits.ts:17`); set it explicitly.

### Headless mode
- Entry: `qwen -p "<prompt>"`, or the prompt on stdin.
- Output: `--output-format text|json|stream-json` (`cli/src/config/top-level-options.ts:359-363`).
  - `stream-json` is JSONL in a **Claude-Code-compatible schema**: `system`, `assistant` (text, thinking, tool_use and tool_result blocks), `user` and `result` with `subtype: success|error_max_turns|error_during_execution`, plus partial `content_block_delta` events (`cli/src/nonInteractive/types.ts:61-268`).
  - `json` writes one array of messages at the end (`nonInteractive/io/JsonOutputAdapter.ts:131`).
- `--json-schema` registers a synthetic `structured_output` tool, and the run ends on the first valid call (`top-level-options.ts:384-390`).
- Approval: `--approval-mode plan|default|auto-edit|auto|yolo`.
  - Headless runs **auto-deny** any tool that needs confirmation (`core/src/core/coreToolScheduler.ts:4650-4669`).
  - `--allowed-tools`, `--core-tools` and `--exclude-tools` shape the tool set.
- Budgets:
  - `--max-session-turns`
  - `--max-wall-time` and `--max-tool-calls`, both exit 55
  - `--max-subagent-depth`, default 5
- Isolation: `--bare` turns off auto-discovery; `--safe-mode` turns off all customisations.
- Exit codes (`core/src/utils/errors.ts:365-409`): 41 auth, 42 input, 44 sandbox, 52 config, 53 turn limit, 54 tool execution, 55 budget, 130 cancel.
- SDKs that drive the CLI over stream-json: `packages/sdk-typescript`, `sdk-python`, `sdk-java`. There is also a `qwen serve` daemon (`cli/src/serve/`), not examined.

### Agent loop
- `core/src/core/agent-loop.ts` defines the stop reasons (`:44-65`): completed, next_round, cancelled, loop_detected, duplicate_provider_call, tool_ended_turn, max_session_turns, session_token_limit and user_prompt_blocked.
- Per user message there is a hard cap of `MAX_TURNS = 100` (`core/src/core/client.ts:260,5153`). `maxSessionTurns` is checked at `:5134`.
- Loop detection (`core/src/services/loopDetectionService.ts:82-179`): repeated tool calls at 5, repeated content at 10, repeated thoughts at 3, file-read stagnation at 8.
- Retries: 7 attempts, starting at a 1.5 s backoff (`core/src/utils/retry.ts:100-102`).
- Repeated provider tool calls are deduplicated (imports in `agent-loop.ts`).
- Truncated tool arguments are repaired and marked. Incomplete file writes are rejected (`core/src/core/incomplete-tool-call-args.ts`).

### Context management
- Source: `core/src/services/chatCompressionService.ts`. It explicitly mirrors Claude Code's `autoCompact.ts` constants.
- Thresholds:
  - Auto-compaction at the lower of 85% of the window and window − 20k − 13k (`:167-182`, `computeThresholds` `:250-300`).
  - Warn tier and hard tier above that.
  - Circuit breaker after 3 consecutive failures (`:200`).
- Summary: an `<analysis>` block, then a 9-section `<state_snapshot>` XML (`core/src/core/prompts.ts:922-941`), with a retained recent tail (`services/retained-compaction-tail.ts`).
- **Microcompaction:** old read, shell, grep, glob and web results are replaced with `[Old tool result content cleared]`, keeping the newest N (`services/microcompaction/microcompact.ts:16,42-54,560`).
- Tool output truncation: 25,000 characters or 1,000 lines by default (`core/src/config/config.ts:1045-1046`). The full output is persisted with a `<persisted-output>` stub and a sha256 (`core/src/tools/truncation.ts:46-78`).

### Tools
About 109 modules in `core/src/tools/`. The relevant ones:
- `read_file` (with offset and limit), `read_many_files`
- `grep_search` (vendored ripgrep), `glob`, `ls`
- **`lsp`**: goToDefinition, findReferences, hover, document and workspace symbols, implementation, call hierarchy (incoming and outgoing), diagnostics, codeActions (`tools/lsp.ts:36-48`)
- `shell` (with background shells), `edit` (prior-read enforcement, `priorReadEnforcement.ts`), `write_file`
- `todo_write`, `agent`/task, `skill`, memory tools, `report_findings`, `record_artifact`, `tool-search` (deferred tool loading), worktree enter and exit, cron, team

### Sub-agents
- The `agent` tool (`tools/agent/agent.ts:279-417`) takes `subagent_type`, `run_in_background` (background by default at top level, foreground when nested), forks that inherit parent turns (`fork_turns`, `fork_tools`, `fork_profile`) and teams.
- Built-in types (`core/src/subagents/builtin-agents.ts:50-395`): general-purpose, Explore, statusline-setup, plus wrappers that delegate to external `claude-code` and `codex` agents.
- Custom agents are markdown files with frontmatter (`subagents/agent-frontmatter-schema.ts`). The exact directory is not verified; it is probably `.qwen/agents/`.

### Memory
- `QWEN.md` and `AGENTS.md` context files (`core/src/utils/memory-constants.ts:7-8`).
- A large managed-memory subsystem (`core/src/memory/`): extract, "dream" consolidation, recall, relevance selection, team memory sync and a secret scanner. It has `manage-memory` and `search-memory` tools. I did not trace its triggers in detail.

### Sandboxing
The strongest of the two projects.
- **Whole-CLI sandbox:** `--sandbox docker|podman|sandbox-exec` (`top-level-options.ts:232-244`).
- **Per-command execution sandbox:** `tools.executionSandbox: {backend: auto|bwrap|landlock, filesystem: read-only|workspace-write, network: open|closed}` (`cli/src/config/execution-sandbox-settings.ts:18-55`; `core/src/sandbox/sandbox-execution.ts`). Landlock uses the vendored runner.
- `plan` approval mode means "analyse only".

### Code review support
Very large.
- A bundled `review` skill (`core/src/skills/bundled/review/SKILL.md`, 1,234 lines, with DESIGN.md at 1,379 lines) backed by `qwen review <subcommand>` tooling (`cli/src/commands/review/`): fetch-pr, parse-args, compose-review, submit, resolve-anchors, dedup-candidates, convergence, thread-lifecycle and more.
- It runs 15–17 parallel specialist agents:
  - issue fidelity,
  - removed-behaviour audit,
  - cross-file caller tracer,
  - security,
  - build and test,
  - personas plus a counter-frame audit,
  - and others.
- After that come dedup, batch verification, an iterative reverse audit, a round-aware posture for re-reviews, and anchors validated offline against hunks.
- Platforms are only `github` (through `gh`) and `aone`, Alibaba's internal GitLab-based host (`cli/src/commands/review/lib/platform/`, `lib/remote-match.ts:37`). There is no public GitLab MR support.
- `packages/channels/gitlab` is a separate component. It polls GitLab todos and @mentions and posts notes (`channels/gitlab/src/GitlabAdapter.ts:206,477`). It does not create inline diff threads.

### Fit for Habodek
- **As the engine: the strongest open candidate if we pick Qwen.**
  - Apache-2.0 licence.
  - Committed vendored binaries.
  - Claude-Code-shaped stream-json, `--json-schema` structured output, and budgets with distinct exit codes.
  - Network-closed shell sandbox.
  - Real LSP tool.
  - Qwen tool-call and think-tag recovery.
  - `reasoning_content` replay.
- Downsides:
  - Usage statistics are **on by default**. They can be switched off, but our wrapper must always set the env var.
  - Very large and fast-moving codebase.
  - Qwen/GLM behaviour is keyed on model or host names.
  - The review pipeline is tied to `gh` and Aone.
- **Single biggest blocker:** default-on telemetry plus the size of the attack surface. We would have to prove zero egress in headless mode with a deny-all proxy. No hard constraint is violated once it is configured.
- **As a source of ideas:** the best review-specific source in this survey.

### Ideas worth stealing
- Review pipeline design (`skills/bundled/review/SKILL.md`):
  - specialist fan-out with per-agent "receipts",
  - a "prove the diff was read" check (Step 3D),
  - dedup, then batch verification of each finding, then a reverse audit,
  - "silence is better than noise",
  - convergence posture for re-reviews (`cli/src/commands/review/lib/convergence.ts`, `thread-lifecycle.ts`),
  - offline anchor validation against hunks (`resolve-anchors.ts`).
- `--json-schema` producing a synthetic `structured_output` tool as the terminator. Ideal for a "findings" contract.
- XML tool-call recovery for Qwen (`xml-tool-call-fallback.ts`).
- Microcompaction that clears old tool results before a full summary (`microcompact.ts`).
- Loop and stagnation detector thresholds (`loopDetectionService.ts:82-179`).
- Per-command bwrap/landlock sandbox with `network: closed` (`execution-sandbox-settings.ts`).

---

## Verdict summary
1. Both projects have permissive licences (MIT, Apache-2.0) and can be made zero-egress by configuration. Neither is disqualified by the hard constraints.
2. opencode switches: `OPENCODE_DISABLE_MODELS_FETCH=1` and `OPENCODE_DISABLE_AUTOUPDATE=1`, put `rg` on PATH, use no plugins, deny web tools. It has no telemetry by default.
3. Qwen Code switches: `QWEN_USAGE_STATISTICS_ENABLED=false` (it is on by default and sends to Aliyun RUM), `QWEN_CODE_MODELS_DEV_REFRESH=off`, exclude web tools.
4. Reasoning replay is correct in both: `reasoning_content` is echoed back on earlier assistant turns. opencode keys it by the observed stream field; Qwen Code keys it by thought parts.
5. In both, the Qwen/GLM tuning is keyed on provider ID, model name or host. Behind LiteLLM, set `limit.context`/`contextWindowSize` and the thinking flags through `body` or `extra_body` yourself.
6. opencode v2 has no LSP (it was removed in the July 2026 rewrite) and no sandbox. Its churn is extreme: about 5.4k commits in 90 days.
7. Qwen Code has an LSP tool (experimental, no downloads) and a bwrap/landlock shell sandbox with network closed. It also has Claude-Code-compatible stream-json, `--json-schema` and budget exit codes.
8. As the embedded engine, Qwen Code beats opencode for Habodek, especially if we pick the Qwen model. opencode is second, held back by missing LSP and its instability.
9. For ideas: take Qwen Code's review skill (fan-out, verify, reverse audit, convergence) and opencode's compaction (checkpoint template plus background precompute).
10. Neither supports public GitLab MR inline threads, so Habodek's thin layer must own GitLab either way.
