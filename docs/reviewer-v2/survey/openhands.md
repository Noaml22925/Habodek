# OpenHands (survey for Habodek)

Paths below are relative to the clones in `<clones>/`. Unless prefixed, `sdk/...` means `openhands-sdk/openhands-sdk/openhands/sdk/...` and `tools/...` means `openhands-sdk/openhands-tools/openhands/tools/...`.

## Identity: the project has split up

- **`All-Hands-AI/OpenHands`** now redirects to `OpenHands/OpenHands`. It is no longer the agent. It is **"Agent Canvas"**, a React/TypeScript/Electron control panel (`openhands/AGENTS.md:5`, `openhands/README.md:5`). The V0 Python core was deleted in April 2026: the resolver in `cc100c0d1` (2026-04-23) and the controller in `180a35f01` (2026-04-24). HEAD is `02e20e47d1a8` (2026-11-23), 1113 commits in the last 90 days, 156 author emails in the last 180 days, latest tag v1.44.0.
- **The thing to embed is `OpenHands/software-agent-sdk`.** The main repo names it as the owner of "Python SDK, Agent Server, agents/tools, conversations, events" (`openhands/AGENTS.md`, ownership table). It is a uv workspace with four packages: `openhands-sdk`, `openhands-tools`, `openhands-workspace` and `openhands-agent-server` (`openhands-sdk/pyproject.toml:3`).
  - HEAD `077f3fad6d31` (2026-11-23); 899 commits in the last 90 days; 112 author emails in the last 180 days. Top committers are core staff, plus dependabot (187) and an "OpenHands Bot" (104).
  - Latest tag v1.63.1 (2026-11-20). Paper: arXiv 2511.03690.
- **`OpenHands/OpenHands-CLI`** is marked "no longer actively maintained" (`openhands-cli/README.md`, commit `954f2ba`, 2026-08-11). 2 commits in 90 days, last tag 1.16.0. Do not build on it.
- **`OpenHands/extensions`** (read at `55bcbb2ea5e6`) holds the skills and plugins, including the PR-review plugin and a GitLab MR reviewer.

## Licence

- All four repos are **MIT** (`openhands-sdk/LICENSE`, `openhands/LICENSE`, `openhands-cli/LICENSE`, `openhands-extensions/LICENSE`). No source-available or enterprise directories are in the SDK. No CLA was found in `CONTRIBUTING.md`.
- The closed or commercial parts are external services that are all opt-in:
  - OpenHands Cloud and the LLM proxy at `llm-proxy.app.all-hands.dev` (`sdk/llm/utils/openhands_provider.py:11`)
  - a hosted "critic" (`sdk/critic/impl/api/client.py:68`)
  - GraySwan (`sdk/security/grayswan/analyzer.py:66`)
  - the DigitalOcean "MARS" UI in Canvas.

## Language, runtime and dependency footprint

- Python ≥3.12. The pinned closure is in `uv.lock` (416 packages across all platforms).
- My parse of `uv.lock`, Linux only: **`openhands-sdk` ≈ 98 packages; SDK + `openhands-tools` ≈ 158.**
- Heavy hard dependencies:
  - `litellm>=1.108.3,<1.109` (`openhands-sdk/pyproject.toml:19`)
  - `lmnr` (Laminar/OTel), `fastmcp`, `fakeredis[lua]`, `tree-sitter(-bash)`
  - In tools: `browser-use` (which pulls google-genai, anthropic, groq, ollama and posthog), `tom-swe` and `libtmux` (`openhands-tools/pyproject.toml:6-19`).
  - Without the browser-use subtree the closure is about 105 packages. Removing it needs a patched or `--no-deps` install, because it is a hard dependency even though it is only imported for browser tools (`tools/__init__.py:13-16`).
- Every package is a PyPI wheel, including the native ones (pydantic-core, tiktoken, tokenizers, grpcio, orjson, tree-sitter, cryptography, fastuuid). A wheelhouse from `uv export` plus `pip download` is straightforward.
- Nothing is downloaded at install time.
- At runtime, `rg` and `tmux` are used **if present**, with fallbacks: rg falls back to grep and then pure Python (`tools/grep/impl.py:49-55`); tmux falls back to a subprocess terminal (`tools/terminal/terminal/factory.py:112-138`). Neither is fetched.
- The repo enforces a 7-day minimum package age (`pyproject.toml:7`).

## Outbound calls and telemetry

| Call | Where | Default | How to disable |
|---|---|---|---|
| **LiteLLM model cost map** from raw.githubusercontent.com, **fetched at `import litellm`**, 5 s timeout plus a background retry thread | litellm 1.108.3 `litellm/__init__.py:430,572`; `litellm_core_utils/get_model_cost_map.py:676-700` | ON | `LITELLM_LOCAL_MODEL_COST_MAP=True`. A failed fetch falls back to the bundled JSON. The SDK never sets this variable; only its own verify script does (`.agents/skills/verify-sdk/scripts/control_sdk.py:139`). |
| LiteLLM Anthropic beta-headers config | `litellm/__init__.py:438`, `anthropic_beta_headers_manager.py:148,195` | lazy, Anthropic paths only | `LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS=True` |
| LiteLLM HF tokenizer download | `litellm/utils.py:2509-2543` | only for llama, cohere and anthropic names; qwen and glm use tiktoken | not needed for Qwen/GLM. tiktoken files are bundled and `TIKTOKEN_CACHE_DIR` is forced to them (`litellm_core_utils/default_encoding.py:17-30`). |
| `GET {base_url}/v1/model/info` against **your LiteLLM gateway** when `model="litellm_proxy/..."`, cached hourly | `sdk/llm/utils/model_info.py:117,190-201` | ON for the `litellm_proxy/` prefix | Stays inside the VPN; this is a feature (see LLM layer). |
| OpenRouter endpoint probe | `sdk/llm/utils/runtime_metadata.py:131-136` | only for OpenRouter models | n/a |
| Public skills: git clone of github.com/OpenHands/extensions | `sdk/skills/skill.py:1172,1295-1356` | **OFF** in SDK (`AgentContext.load_public_skills=False`, `sdk/context/agent_context.py:95`) | also `OH_PUBLIC_SKILLS_ENABLED=false`, or `EXTENSIONS_PATH=<local copy>` (`skill.py:1177-1202`) |
| Laminar/OTel tracing | `sdk/observability/laminar.py:40-45,354-370` | OFF; `lmnr` is not even imported unless `LMNR_PROJECT_API_KEY` or `OTEL_*` is set | leave those env vars unset |
| Agent-server product analytics (PostHog or HTTP) | `openhands-agent-server/.../config.py:171` (exporter default `"none"`), `telemetry/policy.py` | OFF; requires an exporter plus consent | `DO_NOT_TRACK=1` kill switch |
| Canvas frontend PostHog key baked in | `openhands/config/defaults.json:47-48` | consent-gated | irrelevant if we embed the SDK |
| Completion callback POST, LookupSecret GET, image-URL inlining | `sdk/workspace/base.py:152`, `sdk/secret/secrets.py:139`, `sdk/llm/utils/image_inline.py` | only if configured or used | n/a |
| browser-use (has its own posthog dependency) | `openhands-tools/pyproject.toml` | not imported unless browser tools are enabled | `get_default_tools(enable_browser=False)` |

**Offline verdict:** an in-process SDK with `LITELLM_LOCAL_MODEL_COST_MAP=True` and no browser tools showed **no outbound calls other than to the LLM gateway in the code I read**. I did not audit runtime side effects of transitive packages (fastmcp, fakeredis, tom-swe) at import. To check, I would run the process under `unshare -n` or with a deny-all egress proxy and log every connect().

## LLM layer

- **LiteLLM is used as a library.** `litellm.completion` is called from `sdk/llm/llm.py` (3741 lines), and the module sets `litellm.modify_params = True` (`llm.py:172`).
- `LLM(model, base_url, api_key, …)` works with any OpenAI-compatible endpoint. Use `model="litellm_proxy/<alias>"` with the gateway URL, or `openai/<alias>`.
- Defaults: `num_retries=5`, exponential backoff 8–64 s, `timeout=300`, `max_message_chars=30000`, `reasoning_effort="high"`, `native_tool_calling=True`, `drop_params=True` (`llm.py:383-611`).
- On an empty response the call is retried, and a temperature of 0 is bumped to 1.0 (`llm/utils/retry_mixin.py:160-181`).
- **Native vs emulated tool calling.** Native function calling is the default.
  - Setting `native_tool_calling=False` switches to prompt-emulated calls: XML-ish `<function=name><parameter=…>` blocks with stop word `</function` and in-context examples (`llm/mixins/non_native_fc.py:36-108`, `llm/mixins/fn_call_converter.py:49-85,593-605`).
  - `reasoning_content` is preserved through the emulation (`non_native_fc.py:100-102`).
- **Tool schemas get two extra fields.**
  - `security_risk` is injected into every non-read-only tool schema. The agent hardcodes `add_security_risk_prediction=True` (`agent/agent.py:955`; `tool/tool.py:721-736`) and the system prompt gets an `llm_security_analyzer` section (`agent.py:594-606`). That costs extra tokens. Disabling it needs a subclass or patch; I did not verify whether a flag exists.
  - A `summary` field is always added (`tool.py:735`).
- **Reasoning across turns.**
  - `reasoning_content`, Anthropic `thinking_blocks` and Responses reasoning items are parsed from every response (`llm/message.py:491-541`).
  - They are stored on the first `ActionEvent` of a batch and re-attached when history is rebuilt (`event/base.py:179-200`).
  - **They are only echoed back to the API when the model feature `send_reasoning_content` is true** (`message.py:335-337`). That defaults to False and is set only for kimi, deepseek-reasoner/v4, gpt-oss and minimax-m2 (`llm/utils/model_features.py:251-273,439`).
  - **Qwen and GLM do not get it by default.** `glm*` only gets `force_string_serializer` (`model_features.py:250`).
  - For Qwen3/GLM thinking with interleaved tool use, set `capability_overrides={"send_reasoning_content": True}`. This works: `resolve()` honours any key, with caller > endpoint > sdk precedence (`model_features.py:343-362`). The field's docstring does not list this key (`llm.py:486-497`).
  - Alternatively, declare `send_reasoning_content: true` in the gateway's `model_info`. Proxy `/v1/model/info` metadata counts as an "endpoint" declaration (`model_info.py:139-140`, `model_features.py:340-362`).
  - In LiteLLM's request transform I found no stripping of string `reasoning_content` for the `openai` or `litellm_proxy` providers. `hosted_vllm` strips only non-string values (`litellm/llms/hosted_vllm/chat/transformation.py:174-178`). I did not verify this end to end against the gateway.
- **Qwen/GLM-specific code.**
  - GLM-4.6 emits JSON-string lists and dicts in arguments; they are decoded (`agent/utils.py:125-160`).
  - Qwen3.5-Flash emits raw newlines in argument JSON, and reasoning-only responses get a corrective nudge (`tests/sdk/agent/test_tool_call_recovery.py:1-14`, `response_dispatch.py:391-416`).
  - The "verified models" list includes `glm-5.3-flash` and `qwen3.8-flash` (`llm/utils/verified_models.py:90-122`).

## Headless mode

- **SDK (recommended):**
  ```python
  conv = Conversation(agent=Agent(llm, tools, condenser),
                      workspace="/checkout",
                      persistence_dir=...,
                      callbacks=[fn],
                      max_iteration_per_run=500,
                      stuck_detection=True)
  conv.send_message(prompt)
  conv.run()
  ```
  - Signature: `sdk/conversation/conversation.py:64-90`. A string workspace gives a `LocalConversation` with a `LocalWorkspace`: no Docker, a plain process in that directory.
  - Callbacks receive typed pydantic events with a `kind` discriminator (`ActionEvent`, `ObservationEvent`, `MessageEvent`, `AgentErrorEvent`, `Condensation`, `ConversationErrorEvent`, …). Each serialises to JSON.
  - Events persist as one JSON file per event plus `base_state.json` (`conversation/persistence_const.py:4`, `conversation/event_store.py:456`).
  - `run()` does not raise on hitting the iteration cap or getting stuck. It sets `state.execution_status` to `FINISHED`, `ERROR` or `STUCK` (`local_conversation.py:2659-2661`), and raises `ConversationRunError` on LLM or auth failures (`:2802-2846`).
  - The final answer comes from `get_agent_final_response(events)` (`conversation/response_utils.py:11`).
  - **Structured output:** attach a pydantic `response_schema` to `FinishTool` or to any tool, and the arguments are validated on receipt (`examples/01_standalone_sdk/56_structured_output.py`; `openhands-tools/.../preset/default.py:86-97`). This is the natural way to return a typed list of `{file, line, body}` findings.
  - Confirmation mode is off by default, so the run is non-interactive. Restrict tools by choosing the tool list.
  - PreToolUse, PostToolUse and Stop hooks exist, Claude-Code style (`sdk/hooks/types.py:9-17`).
- **Agent server:** `agent-server` (FastAPI) with `POST /api/conversations` taking `{agent:{kind,llm,tools}, workspace:{kind:"LocalWorkspace",working_dir}, initial_message}`, polling `GET /api/conversations/{id}` and `/agent_final_response`. A worked example is `openhands-extensions/skills/gitlab-mr-reviewer/scripts/common.py:210-254`. It also exposes an OpenAI-compatible `/v1/chat/completions` facade (`examples/02_remote_agent_server/15_openai_compatible_gateway.py`).
- **CLI (deprecated):** `openhands --headless --json -t "…"` streams JSONL and auto-approves (`openhands-cli/openhands_cli/argparsers/main_parser.py:80-95`).

## The agent loop

- **Outer loop**, `LocalConversation._run` (`sdk/conversation/impl/local_conversation.py:2670-2846`):
  - It handles PAUSED, STUCK and FINISHED states. Stop hooks can veto FINISHED.
  - It runs the stuck check, then `agent.step()`, then the budget check (`max_budget_per_run`) and the iteration cap (default 500, which emits `MaxIterationsReached`).
- **One step**, `Agent._step` (`sdk/agent/agent.py:859-1060`):
  1. Execute any pending actions.
  2. `prepare_llm_messages(view, condenser)`. If this returns a `Condensation`, emit it and end the step (`:901-910`).
  3. Call `llm.generate(tools=…)`.
  4. `classify_response` sorts the result into TOOL_CALLS, CONTENT, REASONING_ONLY or EMPTY (`agent/response_dispatch.py:55-80`).
- **Stop condition:** a plain-text answer ends the run (`response_dispatch.py:260-287`), as does the `finish` tool.
- **Recovery paths:**
  - Reasoning-only or empty responses get a "use a tool" nudge (`:391-416`).
  - Malformed function calls are fed back to the model as a user message (`agent.py:959-970`).
  - Unknown tools and JSON or pydantic validation errors become an `AgentErrorEvent` that names only the parameter keys (`agent.py:1528-1640`).
  - A content-policy block gets a "rephrase" nudge.
  - A context-window error or a malformed-history error triggers a `CondensationRequest` and a retry (`agent.py:992-1037`).
- **Stuck detector:** repeated action/observation 4×, action/error 3× (nudged once first), monologue 3×, and alternating pattern 6× (`conversation/types.py:226-251`, `conversation/stuck_detector.py:27-36`, `local_conversation.py:899-924`).
- **Parallel tools:** `Agent.tool_concurrency_limit` defaults to 1 (`agent/base.py:315-324`, `agent/parallel_executor.py`).

## Context management and compaction

`LLMSummarizingCondenser` (`sdk/context/condenser/llm_summarizing_condenser.py`):

- **Triggers** (`:140-207`):
  - REQUEST, from a context-window error: HARD.
  - TOKENS: the view exceeds min(`max_tokens`, the agent LLM's effective input limit): HARD.
  - EVENTS: `len(view) > max_size`: SOFT.
- **Defaults** for the default agent and sub-agents are `max_size=80`, `keep_first=4` (`:556-566`).
- **What is forgotten** (`:282-356`):
  - The suffix is cut to half the view for a request, `max_size/2` for the event count, or enough events to fall under half the token cap.
  - The system prompt and the first `keep_first` events are protected.
  - Cut points snap to "manipulation indices" so tool-call/result pairs and batches are never split (`context/view/manipulation_indices.py`, `context/view/properties/tool_loop_atomicity.py`).
- **Summary:** a separate LLM call with a structured template (USER_CONTEXT, TASK_TRACKING, COMPLETED, PENDING, CODE_STATE, TESTS, CHANGES, DEPS, VERSION_CONTROL_STATUS) (`condenser/prompts/summarizing_system.j2`). The summary is inserted where the forgotten events were. Earlier summaries are re-summarised, which makes it rolling.
- **Failure handling:**
  - A soft failure returns the view unchanged.
  - A hard failure does a "hard context reset" that summarises everything after the system prompt, shrinking each event string by 20% per retry, up to 5 times (`:358-409`, `condenser/base.py:163-202`).
- `PipelineCondenser` and `NoOpCondenser` also exist. **A bare `Agent()` has no condenser** (`agent/base.py:288`); use `get_default_condenser`.
- **Token counting** uses `litellm.token_counter` (tiktoken for qwen and glm) or an optional HF chat-template tokenizer (`llm.py:3425-3465`, `llm/_tokenizer.py`).
- **Truncation:**
  - Terminal output is truncated middle-out at 30 000 chars, with the full output saved to a file the agent can read (`tools/terminal/constants.py`, `tools/terminal/definition.py:193-198`; `sdk/utils/truncate.py:50-110`).
  - File-editor views are capped at 16 000 chars (`tools/file_editor/utils/constants.py:1`).
  - Every tool message has a hard cap of 50 000 chars (`sdk/utils/truncate.py:12`, `llm/message.py:481-489`).

## Tools

- **Default preset:** `terminal`, `file_editor` and `task_tracker`, plus the built-ins `finish` and `think` (`openhands-tools/.../preset/default.py:37-68`).
  - `terminal` is a persistent tmux session with timeouts and soft no-output timeouts (`tools/terminal/constants.py`).
  - `file_editor` supports view (with line numbers), create, str_replace, insert and undo (`tools/file_editor/definition.py:26`).
- **Optional tools:**
  - `grep` returns only file paths: `rg -l -i`, at most 100 files (`tools/grep/impl.py:37,230-245`).
  - `glob`; `apply_patch` (GPT-5); Gemini-style read_file and edit.
  - `browser_use`; `task` and `delegate` (sub-agents); `ask_oracle` (a second model); `switch_llm`; `tom_consult`; `vision_inspect`.
  - MCP servers through fastmcp.
- **Missing:** no LSP, find-references, call graph or tree-sitter repo map. tree-sitter is used only to parse bash for the security analyser (`sdk/security/_shell_ast.py`). Code navigation is grep, sed and git through the terminal.

## Sub-agents

- The `TaskToolSet` is a Claude-Code-style `task(description, prompt, subagent_type, resume)` (`tools/task/definition.py:51-64`).
- Each sub-agent runs as a **fresh `LocalConversation` with its own context**, in the same working directory, with a copy of the parent LLM (streaming off, separate metrics) (`tools/task/manager.py:308-347,369-404`).
- It inherits the parent's iteration and budget caps, and its tools must be a subset of the parent's (`manager.py:264-286,392-400`).
- It returns `get_agent_final_response`. A non-FINISHED status comes back as an error that carries the partial output (`manager.py:411-440`).
- Tasks are resumable by id and run in parallel through `tool_concurrency_limit`.
- Built-in agent types are markdown files with frontmatter (`preset/subagents/code_explorer.md`, `bash_runner.md`, …). Custom types use `register_agent()`.

## Memory

- Project guidance is loaded from `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` and `.cursorrules`, including nested directory-scoped files. This is opt-in via `load_project_skills` (`sdk/skills/skill.py:351-357,1078-1150`; `agent_context.py:121`).
- Opt-in two-tier `MEMORY.md` (user and project) is injected into the system prompt and maintained by the agent (`examples/01_standalone_sdk/55_persistent_memory.py`).
- There is no vector store.

## Sandboxing

- `LocalWorkspace` has **no sandbox**: it runs as the current user with the full network.
- Isolation comes from `openhands-workspace`: Docker, Apptainer, a remote API or Kubernetes agent-sandbox. Each runs the agent-server inside the sandbox.
- There is no landlock, bwrap or seatbelt, and no read-only mode.
- Mitigations: confirmation policies, LLM or pattern-based security analysers (`sdk/security/`), PreToolUse hooks, and tool scoping.
- For Habodek we would wrap the process ourselves (bwrap, or a container with `--network none` except the gateway) and pass read-only tools.

## Code review support

- The V0 GitHub "resolver" has been removed.
- **`openhands-extensions/plugins/pr-review`** is a GitHub Action built on the SDK.
  - Script: `scripts/agent_script.py:1131-1238`. It creates `Agent(get_default_tools(enable_browser=False), get_default_condenser)` plus an optional `file_reviewer` sub-agent fan-out (`:1090-1128`, `tool_concurrency_limit=4`). The agent posts inline comments via the GitHub API from the terminal.
  - Its prompt (`scripts/prompt.py:96-140`) is worth reading:
    - read AGENTS.md first;
    - "inspect how the affected feature works on the current base… check every affected caller";
    - "Post a finding only when you can state a concrete failure on the current head and cite the code";
    - nonce-fenced untrusted PR content;
    - previous threads are supplied so it does not repeat them;
    - per-file (8k) and total (100k) diff budgets, with an "omitted" marker telling the agent to read the file itself.
  - It ships an evaluator (`scripts/evaluate_review.py`).
- **`skills/gitlab-mr-reviewer`** is a GitLab worker that checks out the exact head SHA into a credential-free directory, starts an agent-server conversation, and posts **one note per head SHA**, not diff-line threads (`scripts/common.py:122-131,283-350`).
- Review skills: `code-review`, `github-pr-review`, `learn-from-code-review`.

## Fit for Habodek

1. **As the embedded engine: viable, and the strongest Python candidate so far.**
   - For: MIT throughout; in-process SDK with no Docker; native tool calling through LiteLLM with a base_url; typed JSON events and a schema-validated finish tool; a robust loop (nudges, malformed-call feedback, stuck detection, context-window recovery); an atomic-unit rolling condenser; Claude-Code-like sub-agents; and an existing PR and MR review reference implementation.
   - Costs:
     - (a) A large Python dependency tree, about 158 wheels, including browser-use, which is never used here.
     - (b) `LITELLM_LOCAL_MODEL_COST_MAP=True` is mandatory, or every import attempts a GitHub fetch.
     - (c) Qwen/GLM need `capability_overrides={"send_reasoning_content": True}`, otherwise reasoning is dropped between tool turns.
     - (d) Every tool call carries a forced `security_risk` and `summary` field, which adds tokens and is a potential failure point for flash models.
     - (e) Very high churn (about 10 commits a day, minor releases weekly), so pin a version and vendor it.
     - (f) No code-intelligence tools beyond grep through the shell.
     - (g) No built-in sandbox.
   - **Single biggest blocker:** none are hard blockers. The most likely practical issue is reasoning-content replay and tool-call reliability with Qwen/GLM flash. It needs an end-to-end test through the real gateway, which I have not done.
2. **As a source of ideas:** high (see the list below).

## Ideas worth stealing

- **Response classification with targeted nudges:** TOOL_CALLS, CONTENT, REASONING_ONLY or EMPTY. A reasoning-only response gets a "use a tool" message instead of ending silently (`sdk/agent/response_dispatch.py:55-80,391-416`).
- **Malformed tool calls go back to the model** with parameter names only, never values, plus argument repair for JSON-in-string and chunked strings (`agent.py:1528-1640`, `agent/utils.py:125-200`).
- **Condenser cut points that never split a tool_call/tool_result pair or a parallel batch** (`context/view/manipulation_indices.py`), plus a hard-reset fallback with progressive event shrinking (`llm_summarizing_condenser.py:358-409`).
- **A context-window error becomes a condensation request and a retry** rather than a failure (`agent.py:1020-1037`).
- **Middle-out truncation that saves the full output to a file** the agent can read or grep later (`sdk/utils/truncate.py:50-110`).
- **A stuck detector that nudges once before declaring STUCK** (`stuck_detector.py`, `local_conversation.py:899-924`).
- **A schema-typed `finish`**, so the host gets validated findings to post as GitLab threads instead of letting the agent call GitLab (`56_structured_output.py`).
- **The PR-review prompt discipline:** concrete failure plus citation, map callers on the base design first, nonce-fenced untrusted MR text, prior-thread context, diff budgets with "read the file yourself" markers (`openhands-extensions/plugins/pr-review/scripts/prompt.py:96-140`).
- **Per-file reviewer sub-agents** with a strict JSON output skill, while the coordinator owns the API (`agent_script.py:1090-1128`).
- **Capability resolution order** of caller, then endpoint (gateway `/model/info`), then SDK table, then LiteLLM guess. Our gateway could declare per-model quirks centrally (`model_features.py:318-445`, `model_info.py:107-160`).
