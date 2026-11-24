# Gateway models survey: "qwen3.8 flash next" and "GLM5.3 flash" behind LiteLLM

Prepared 2026-11-24 for the agentic code reviewer design doc (OpenAI-compatible `/v1/chat/completions` with native `tools`, long sessions).

## How to read this

- **Code citations** are `path:line` in shallow clones under
  `<clones>/`. Clones were read only, never executed:
  - `vllm` @ 51bb4b58 (2026-11-23)
  - `sglang` @ 563813c0 (2026-11-23)
  - `litellm` @ a527f1a7 (v1.112.0, 2026-11-23)
  - `qwen-latest` = QwenLM/Qwen3.8 @ 2ea10dc7 (2026-08-17)
  - `glm-5` = zai-org/GLM-5 @ 83c03d31 (2026-10-15)
  - `glm-4.5` = zai-org/GLM-4.5 @ 170f20b2 (2026-02-01)
  - `qwen-code` @ c134a03e (2026-11-23)
- **Templates.** Hugging Face was blocked from this machine, so the actual chat templates came from copies vendored in the vLLM and SGLang repos:
  - SGLang parity fixtures record the HF revision they were taken from.
  - The extracted copies are `survey/qwen38_flash_next_template.jinja` (from `Qwen/Qwen3.8-Flash-Next`, HF revision `de4b8e4d`) and `survey/glm53_flash_template_sglang.jinja` (from `zai-org/GLM-5.3-Flash`, HF revision `eb9eb208`).
  - Line numbers marked "Qwen tpl" or "GLM tpl" refer to those two files. "GLM fixture" means `repos/vllm/rust/src/chat/src/renderer/glm5_next/fixtures/glm_5_3_flash.jinja`. It is identical to the SGLang copy except for the tool-result ordering block (line 181 on).
- **Web sources.**
  - **[web]** means I read the page itself (github.com pages could be fetched).
  - **[search]** means I saw only a search-engine summary and did not read the page. Treat these as **not verified**.
  - Blocked hosts: huggingface.co, alibabacloud.com, ollama.com, the-decoder.com and thenewstack.io.
- **Anything without a citation is marked "not verified".**

---

## 1. Which public models the names probably are

**The mapping is unconfirmed.** The internal names match two public releases almost word for word, but nothing here proves the gateway serves those exact weights. It could serve hosted APIs instead, or quantized checkpoints (FP8, NVFP4 or MXFP4), and several of the bugs in §4 and §6 depend on the quantization or engine.

| | **Qwen3.8-Flash-Next** (likely "qwen3.8 flash next") | **GLM-5.3-Flash** (likely "GLM5.3 flash") |
|---|---|---|
| HF repo | `Qwen/Qwen3.8-Flash-Next`; `-FP8`; third-party NVFP4/MXFP4 (`repos/vllm/recipes/models/Qwen/Qwen3.8-Flash-Next.yaml:28,86,114,151`) | `zai-org/GLM-5.3-Flash` (FP8, the default); `-BF16`; RedHatAI NVFP4 (`repos/glm-5/README.md:79-80`; `repos/vllm/recipes/models/zai-org/GLM-5.3-Flash.yaml:23,92,97`) |
| Released | vLLM recipe added 2026-08-26 (`Qwen3.8-Flash-Next.yaml:6`) | vLLM recipe added 2026-08-27 (`GLM-5.3-Flash.yaml:6`). Press says 26 Aug [search] |
| Params | 125B main model + 51B N-gram embedding table = 176B total, **6B active** (`Qwen3.8-Flash-Next.yaml:5,32-33,224-227`; `repos/sglang/docs/cookbook/autoregressive/Qwen/Qwen3.8-Flash-Next.mdx:305`) | **320B total / 18B active**: 45 layers, 8 of 288 experts per token, one MTP layer (`repos/glm-5/README.md:79`; `GLM-5.3-Flash.yaml:35-36,188-192`) |
| Architecture | "Qwen4Exp", a Qwen4 preview: 3 Gated DeltaNet layers to 1 Qwen Sparse Attention layer, gated residual, N-gram embedding, MTP (`Qwen3.8-Flash-Next.yaml:229-238`; `repos/vllm/docs/design/mm_processing.md:109`). Text and image input | Hybrid of KDA linear attention and NoPE sparse MLA, plus mHC. New base model, not GLM-5.3 (`repos/glm-5/README.md:28`; `GLM-5.3-Flash.yaml:188-192`). Text, image and video input |
| Context | 262,144 native; 1M with static YaRN, opt-in (`Qwen3.8-Flash-Next.yaml:34,64-72,399-403`). A single 262K-token request "was not tested" (`:401-403`) | 1,048,576 declared (`GLM-5.3-Flash.yaml:37,192`). One host lists 256K, so providers can cap it lower [search] |
| Licence | "Qwen Community License 1.0" according to secondary sources [search]. The sister model Qwen3.8-27B is reportedly Apache-2.0 [search]. Secondary sources disagree on restrictions: one says the licence asks AI coding-assistant or API businesses for a separate deal. **Not verified. Legal must read the LICENSE file** (SGLang only links it: `Qwen3.8-Flash-Next.mdx:316`) | MIT according to several secondary sources [search]. Flagship GLM-5.3 reportedly uses a separate "GLM-5.3 license" [search]. **Not verified.** The repo LICENSE (Apache-2.0, `repos/glm-5/LICENSE:1`) covers code, not weights |
| Hosted twin | `qwen3.8-flash` on QwenCloud (`Qwen3.8-Flash-Next.mdx:305`) | GLM-5.3-Flash on the Z.ai API (`repos/glm-5/README.md:11`); its API model id is not verified |

### Neighbouring releases, in case the names map elsewhere

**Qwen**
- **Qwen3.8 line:** 2.4T-A95B and 27B dense (`repos/qwen-latest/README.md:48-49`).
- **Qwen3.7-Flash:** hosted only, snapshot 2026-07-15 [search].
- **Qwen3.6-35B-A3B:** 262K context, Apache 2.0 (`repos/sglang/docs/cookbook/autoregressive/Qwen/Qwen3.6.mdx:16,72`). Sold as the API `qwen3.6-flash` [search].
- **Qwen3.5 line:** released Feb–Mar 2026, including 35B-A3B (`repos/qwen-latest/README.md:52-54`).
- **Qwen3-Next-80B-A3B:** 2025-09-11, 262K context, `hermes` tool parser (`repos/vllm/recipes/models/Qwen/Qwen3-Next-80B-A3B-Instruct.yaml:6,22-24,33-34`).

**GLM**
- **Flagships:** GLM-5.3, 5.2, 5.1 and 5, all 744B-A40B (`repos/glm-5/README.md:77-86`).
- **GLM-4.7-Flash:** 30B-A3B. **GLM-4.5-Air:** 106B-A12B. **GLM-4.5/4.6/4.7:** 355B-A32B (`repos/glm-4.5/README.md:81-88`).
- **Context:** GLM-4.7 has 202,752 (`repos/vllm/recipes/models/zai-org/GLM-4.7.yaml:25`).
- **Licence:** GLM-4.5 is MIT (`repos/glm-4.5/README.md:69`).

Neither gateway model is small. "Flash" is a price tier: 6B and 18B **active** parameters on 176B and 320B MoE bodies.

---

## 2. Thinking-mode controls

### Qwen3.8-Flash-Next

**Defaults and switches**
- **On/off.** Thinking is on by default. Send `chat_template_kwargs: {"enable_thinking": false}` to pre-fill an empty `<think>\n\n</think>` (Qwen tpl 163-169; `Qwen3.8-Flash-Next.mdx:369`). vLLM also sets `enable_thinking=false` when top-level `reasoning_effort` is `"none"` (`repos/vllm/vllm/entrypoints/openai/chat_completion/protocol.py:675-676`).
- **`reasoning_effort`.** Accepts `xhigh` (the default), `medium` or `low`. **Any other value makes the template raise** (Qwen tpl 46-50). The effort turns into a sentence in the system prompt (Qwen tpl 51-55, 59-60, 80-85).
  - vLLM forwards top-level `reasoning_effort` into the template kwargs (`protocol.py:659-667`).
  - A template exception becomes **HTTP 400** (`repos/vllm/vllm/renderers/hf.py:938-944`).
  - The owner can rewrite values server-side with `--reasoning-effort-map` (`repos/vllm/vllm/entrypoints/common/cli_args.py:110-115`; `repos/vllm/docs/features/reasoning_outputs.md:244-250`).

**What not to send**
- **Avoid LiteLLM `thinking: {type: enabled, budget_tokens}` for Qwen.** For `hosted_vllm`, LiteLLM converts it into `reasoning_effort` of `high`, `medium`, `low` or `minimal` (`repos/litellm/litellm/llms/hosted_vllm/chat/transformation.py:105-112`; `repos/litellm/litellm/litellm_core_utils/reasoning_effort_utils.py:69-83`). `high` and `minimal` would then get a 400 from this template.

**Thinking budget**
- vLLM has a per-request `thinking_token_budget`. It needs `--reasoning-parser` and possibly `--reasoning-config` (`protocol.py:285`; `reasoning_outputs.md:267-299`). Not verified for this model.

**Where the reasoning comes back**
- **Field name.** With `--reasoning-parser qwen3`, as in the Qwen recipes (`Qwen3.8-Flash-Next.yaml:50-54`; `repos/qwen-latest/README.md:158,171`):
  - vLLM returns the field `reasoning` (`repos/vllm/vllm/entrypoints/openai/chat_completion/protocol.py:93`).
  - SGLang returns `reasoning_content` (`repos/sglang/python/sglang/srt/entrypoints/openai/protocol.py:1414`).
  - LiteLLM normalises both to `reasoning_content` (§5a).
- **No reasoning parser on the server.** The prompt already ends in `<think>\n` (Qwen tpl 168), so output looks like `...reasoning</think>answer` with no opening tag. LiteLLM's fallback regex needs a leading `<think>` (`re.match`, `repos/litellm/litellm/litellm_core_utils/prompt_templates/reasoning_utils.py:336-357`), so **reasoning would leak into `content`**.
- **Cut off at `max_tokens`.** If generation stops inside thinking, `content` is empty and only `reasoning_content` holds text (`Qwen3.8-Flash-Next.mdx:404`).

### GLM-5.3-Flash

**Thinking cannot be switched off**
- The generation prompt always ends in `<|assistant|><think>` (GLM fixture 255-257), and the template has no `enable_thinking` variable. The vLLM recipe says "Thinking is always on" (`GLM-5.3-Flash.yaml:358`).
- **Conflict:** the SGLang cookbook says `chat_template_kwargs: {"thinking": false}` turns it off (`repos/sglang/docs/cookbook/autoregressive/GLM/GLM-5.3-Flash.mdx:421`). The template contradicts this, and so does sglang#36669: "`enable_thinking` / `thinking: False` has no effect, because the template ignores it" [web, https://github.com/sgl-project/sglang/issues/36669].
- vLLM maps an Anthropic-style `thinking.disabled` to a low effort for models that cannot turn thinking off (`reasoning_outputs.md:449-452`).

**`reasoning_effort`**
- Accepts `low`, `high` or `max`. The default is `max`, and **any other value silently becomes `max`** (GLM fixture 2-3; `repos/vllm/rust/src/chat/src/renderer/glm5_next/encoding.rs:314-318`; `repos/glm-5/README.md:111-112`).
- It is rendered as `<|system|>Reasoning Effort: X`.
- Pass it top-level (vLLM inserts it into the template kwargs: `repos/vllm/rust/src/chat/src/renderer/glm5_next/mod.rs:51-52`) or through `chat_template_kwargs` (`GLM-5.3-Flash.yaml:368-371,383-388`).
- So a LiteLLM `thinking` budget that maps to `medium` silently buys the most expensive mode.

**Parser and output field**
- vLLM uses `--reasoning-parser glm47` and `--tokenizer-mode glm5_next` (`GLM-5.3-Flash.yaml:38-40,53-57`). SGLang resolves to `glm45` (`GLM-5.3-Flash.mdx:176,419`).
- The output field depends on the engine, as for Qwen.

---

## 3. Multi-turn tool use with thinking ("preserved" / "interleaved")

**Both models expect every earlier turn's reasoning to be sent back by default.** This differs from Qwen3.

### Qwen3: your recollection is correct

For Qwen3, the template keeps reasoning only for assistant turns after the last real user query: `{%- if loop.index0 > ns.last_query_index %}` (`repos/vllm/rust/src/chat/tests/templates/qwen3.jinja:17-22,43-49`). Qwen3.5-0.8B uses the same rule (`.../templates/qwen35.jinja:100-101`). Both templates also parse `<think>…</think>` out of `content` when `reasoning_content` is missing (`qwen3.jinja:34-41`).

### Qwen3.8-Flash-Next

**Template behaviour**
- **Preserve switch.** It is `preserve_thinking is undefined or preserve_thinking is true or loop.index0 > ns.last_query_index` (Qwen tpl 116). The default therefore **keeps reasoning on every historical assistant turn**. `preserve_thinking: false` restores Qwen3 behaviour. SGLang confirms "Thinking blocks are preserved by default" (`Qwen3.8-Flash-Next.mdx:371`).
- **Source of reasoning.** It reads only `message.reasoning_content` (Qwen tpl 111-115) and does **not** parse `<think>` from content. A turn sent without it renders as an empty `<think>\n\n</think>`.
- **Last user query.** User messages that are pure `<tool_response>` do not count as the last query (Qwen tpl 88-98).

**What Alibaba's own harness does.** Qwen Code always sends `preserve_thinking: true` to DashScope (`repos/qwen-code/packages/core/src/core/openaiContentGenerator/provider/dashscope.ts:483-486,513-516`). Qwen3.6 introduced this as an opt-in kwarg (`Qwen3.6.mdx:568-585`). Its default on 3.6 is not verified.

### GLM-5.3-Flash

**Template behaviour**
- **Clear switch.** `clear_thinking` defaults to `false` (GLM fixture 4). Reasoning is rendered for every past assistant turn unless `clear_thinking` is true **and** the turn comes before the last user message (GLM fixture 132-137, 149-153).
- **Source of reasoning.** It reads `reasoning_content`, or else splits `<think>…</think>` out of content (GLM fixture 143-148). Missing reasoning renders `<think></think>`.

**Vendor guidance**
- `repos/glm-5/README.md:113`: "`clear_thinking` defaults to `false` … For chat scenarios, explicitly pass `clear_thinking=true`." Agents should keep the default.
- The GLM-4.7 line describes "Interleaved", "Preserved" and "Turn-level" thinking. For agentic tasks it recommends `{"enable_thinking": true, "clear_thinking": false}` (`repos/glm-4.5/README.md:33-40,209-216`).

### Engines

- **vLLM** accepts `reasoning_content` or `reasoning` on input. It renames the first to `reasoning` (`repos/vllm/vllm/entrypoints/openai/chat_completion/protocol.py:586-611`) and gives the template both names "for interleaved thinking" (`repos/vllm/vllm/renderers/chat_utils/chat_message.py:667-672`).
- **SGLang** checks whether the template reads `reasoning_content` (`repos/sglang/python/sglang/srt/entrypoints/openai/serving_chat.py:476-477,518-522,3457-3466`).

### Harness implication

1. Echo each assistant turn's reasoning string back as `reasoning_content`, including on tool-call turns.
2. Expect context to grow by the reasoning tokens.
3. Whether byte-identical echo is needed for prefix-cache hits is **not verified**. The Qwen template `trim`s the reasoning (tpl 115).

---

## 4. Tool-call formats, parsers and known failure modes

### Formats and parsers

| | Qwen3.8-Flash-Next | GLM-5.3-Flash |
|---|---|---|
| Wire format | Qwen3-Coder XML: `<tool_call>\n<function=NAME>\n<parameter=K>\nV\n</parameter>\n</function>\n</tool_call>` (Qwen tpl 68, 121-145) | `<tool_call>NAME<arg_key>K</arg_key><arg_value>V</arg_value>…</tool_call>`. Results are grouped after `<|observation|>` and re-ordered to match `tool_call` ids (GLM fixture 50, 157-163, 165-250) |
| vLLM parser | `qwen3_coder` (alias `qwen3_xml`), with `--enable-auto-tool-choice` (`Qwen3.8-Flash-Next.yaml:44-49`; `repos/vllm/vllm/tool_parsers/__init__.py:210-212,221-223`) | `glm47` (`GLM-5.3-Flash.yaml:47-52`) |
| SGLang parser | `qwen3_coder` (`Qwen3.8-Flash-Next.mdx:125`) | `glm47` tool parser with `glm45` reasoning parser (`GLM-5.3-Flash.mdx:176-177`) |
| Wrong choice | `hermes` expects JSON inside `<tool_call>`. Its vLLM alias is `qwen3_json` (`__init__.py:214-219`), and it is meant for Qwen2.5 and Qwen3-era models, not this XML format. Empirical failure not tested | The vLLM `glm47` parser has `supports_required_and_named = False`, so `tool_choice: "required"` or a named tool falls back to auto parsing (`repos/vllm/vllm/tool_parsers/glm47_moe_tool_parser.py:11`; `repos/vllm/vllm/parser/abstract_parser.py:410-423,455-457`). It constrains the call envelope by default (`glm47_moe_tool_parser.py:13-14`; `repos/vllm/vllm/tool_parsers/tool_strict_level.py:10-21`) |

### Request-shape traps visible in the templates and vLLM

- **Mid-conversation system messages.** The Qwen template raises "System message must be at the beginning." for any system message after index 0, and raises "No user query found" if there is no real user turn (Qwen tpl 99-106). Both become a 400 under vLLM. The GLM template accepts system messages anywhere (GLM fixture 251-252).
- **Tool-call arguments must be a mapping.** vLLM parses OpenAI JSON-string arguments into a dict. Arguments that are malformed or not an object are **silently coerced to `{}`** with only a server log warning (`repos/vllm/vllm/renderers/chat_utils/chat_message.py:711-783`). This corrupts history without any error, so validate arguments before echoing them back.
- **`content: null` on GLM.** An assistant message with `content: null` plus `tool_calls` rendered a literal "None" on GLM-5.3-Flash. This caused degenerate loops after about 8–20 tool turns. It was fixed in vLLM by #54368 and the issue closed on 2026-08-30 [web, https://github.com/vllm-project/vllm/issues/54337]. Send `""` anyway, since the gateway's vLLM version is unknown.
- **Parallel calls.** vLLM defaults `parallel_tool_calls` to `True` (`protocol.py:287`). Both templates render multiple calls and multiple results. Parse reliability for parallel calls is **not verified**.

### Reported failures (newest state as read)

- **Qwen3.8-Flash-Next on SGLang: token-0 loop.**
  - With thinking on, at least one tool and `qwen3_coder`, the model loops on token 0 (`!`) until `max_tokens` and never emits a tool call.
  - Closed 2026-09-25, fixed by #36806 via #37500. Workarounds in the thread: thinking off, FP8 instead of NVFP4 KV cache, disabling speculative decoding [web, https://github.com/sgl-project/sglang/issues/36537].
- **Qwen3 parser in vLLM: fenced code is parsed as a call.**
  - A `<tool_call>` inside a fenced code block becomes a real call and truncates the text. Still open; PR #57553 [web, https://github.com/vllm-project/vllm/issues/57541].
  - **Directly relevant to a code reviewer that quotes code.** A similar inline-code case is reported in a fork [search, https://github.com/jpezzulli/sglang-rtxpro6000/issues/178].
- **Qwen3.8-Flash-Next NVFP4 with `qwen3_xml` and MTP: garbled tool name.**
  - Tool calls broke after a few calls in long opencode sessions, giving a tool name like `b=b=b </parameter>`.
  - Open, root cause unknown. A short-prompt probe after a rebuild showed 0 malformed calls in 97 [web, https://github.com/colonel-otto/3x-dgx-spark-mesh-qwen-3.8-flash-next/issues/3].
- **Qwen3.8-Flash-Next: empty turns after tool results.** "No response requested." [search, HF discussion #50]. Not verified.
- **GLM-5.3-Flash on SGLang: thinking degenerates into `!`.**
  - Seen under multi-tool agentic prompts, with empty `content`. Still open, no fix.
  - A later report in the thread: after one ~1M-token request, every later request degenerates until the server restarts [web, https://github.com/sgl-project/sglang/issues/36669].
  - The same symptom was seen through **LiteLLM proxy → SGLang** in opencode [web, https://github.com/anomalyco/opencode/issues/45533].
- **GLM-5.3-Flash with SGLang HiCache: dropped calls.**
  - Host-tier load-back dropped tool calls (`finish_reason=stop`, `tool_calls=null`, hallucinated tool output) and caused repetition loops at 100K–230K-token prompts.
  - Fix PR #38212; the issue was closed by an inactivity bot [web, https://github.com/sgl-project/sglang/issues/38031].
- **GLM-5.3-Flash: other symptoms.**
  - "Announces parallel tool calls but never emits one" under context pressure [search, https://github.com/Enntity/sparkglm/issues/55].
  - `tool_calls` finish_reason with no usable call [search, https://github.com/IngeniousIdiocy/ds4/issues/1].
  - Both not verified.

---

## 5. LiteLLM (v1.112.0) behaviour for `openai/`, `hosted_vllm/` and `openai_like`

### Routing

- **`hosted_vllm/`** goes through `_complete_hosted_vllm` to `base_llm_http_handler`, using `HostedVLLMChatConfig` (`repos/litellm/litellm/main.py:2877-2918,6758-6759`).
- **`openai/` with a non-OpenAI model name** goes through `_complete_custom_openai` using the OpenAI SDK, with `OpenAIUnknownModelConfig` (`main.py:6765-6791`; `repos/litellm/litellm/utils.py:10467-10478`; `repos/litellm/litellm/llms/openai/chat/gpt_transformation.py:1058-1064`).
- **`openai_like`** is annotated "embedding only" (`repos/litellm/litellm/types/utils.py:4754`). Chat for it runs only through JSON-registered providers (`main.py:3515,3551`). Do not expect it on the chat route. Not verified further.

### (a) `reasoning_content` in responses: passed through

- **Non-streaming.** `convert_to_model_response_object` calls `extract_reasoning_content`, which takes `reasoning_content`, or else `reasoning` (vLLM's name), or else a leading `<think>` in content (`repos/litellm/litellm/litellm_core_utils/prompt_templates/reasoning_utils.py:23-40`; `repos/litellm/litellm/litellm_core_utils/llm_response_utils/convert_dict_to_response.py:912-913`). `Message.reasoning_content` is kept whenever it is not None (`types/utils.py:1554,1604-1605,1630-1633`).
- **Which code path.** For hosted_vllm the call goes through `gpt_transformation.py:880-927`; for `openai/` through `repos/litellm/litellm/llms/openai/openai.py:939`.
- **Streaming.** `reasoning` becomes `reasoning_content` (`gpt_transformation.py:1068-1086,1117`; `Delta` in `types/utils.py:1700-1705`).
- **Unknown extra fields** land in `provider_specific_fields` (`convert_dict_to_response.py:904-907`).
- **Gateway option to ask about:** `merge_reasoning_content_in_choices` folds reasoning back into `content` (`repos/litellm/litellm/litellm_core_utils/streaming_handler.py:571,1603`).

### (b) Assistant `reasoning_content` or `thinking_blocks` in requests

- **`hosted_vllm`:**
  - Drops `thinking_blocks`.
  - Drops `reasoning_content` **only if it is not a string**; a string is forwarded.
  - Flattens Anthropic-style content lists (`repos/litellm/litellm/llms/hosted_vllm/chat/transformation.py:176-238`, specifically 182-186).
- **`openai/`:**
  - Forwards messages unchanged. There is no stripping in `OpenAIGPTConfig._transform_messages` (`gpt_transformation.py:476-489`).
  - The generic `validate_and_fix_openai_messages` only removes None-valued keys and JSON-encodes `tool_calls` (`utils.py:9918-9992`).
  - So both `reasoning_content` and `thinking_blocks` reach vLLM. What vLLM does with `thinking_blocks` is not verified.
- **Unknowable from code:** custom callbacks or guardrails on the gateway could still alter messages. Test this (checklist item 5).

### (c) `chat_template_kwargs` and `extra_body`: passed through

- For `openai` and every `openai_compatible_providers` entry (which includes `hosted_vllm`, `repos/litellm/litellm/constants.py:1151`), any parameter LiteLLM does not know is moved into `extra_body` (`utils.py:6577-6620`). Examples: `chat_template_kwargs`, `top_k`, `min_p`, `repetition_penalty`, `thinking_token_budget`.
- **`hosted_vllm`** merges `extra_body` into the top-level JSON (`repos/litellm/litellm/llms/custom_httpx/llm_http_handler.py:1147,1186-1195`).
- **`openai/`** hands it to the SDK's `extra_body`, which merges it the same way (`openai.py:441-452`).
- There is no special handling of `chat_template_kwargs` for these providers; it is only special-cased for together_ai and fireworks_ai (grep).
- `reasoning_effort` is a supported parameter for both (`hosted_vllm/chat/transformation.py:78-81`; `gpt_transformation.py:1063-1064`).

### (d) `drop_params`; `max_completion_tokens` versus `max_tokens`

- **Default.** It comes from env `LITELLM_DROP_PARAMS` (`repos/litellm/litellm/__init__.py:233`; `repos/litellm/litellm/litellm_core_utils/core_helpers.py:435`).
- **Behaviour.** An OpenAI-named parameter outside the provider's supported list raises `UnsupportedParamsError` (status 500) unless `drop_params`; with it, the parameter is **silently dropped** (`utils.py:5981-6020`). The supported list is `gpt_transformation.py:263-301`, plus `reasoning_effort`, plus `thinking` for hosted_vllm only.
  - For `openai/`, a `thinking` parameter therefore errors or vanishes. This is inferred from code, not run.
- **Token limits.**
  - LiteLLM forwards `max_tokens` and `max_completion_tokens` unchanged for these providers (`gpt_transformation.py:269-270`) and does no conversion.
  - vLLM prefers `max_completion_tokens` when both are set and marks `max_tokens` deprecated (`repos/vllm/vllm/entrypoints/openai/chat_completion/protocol.py:253,695-701`).
  - With neither set, output is capped by `max_model_len` minus the prompt, or by `max_new_tokens` in the model's `generation_config` (`repos/vllm/vllm/config/model.py:374-380`).
- **Empty tools.** `hosted_vllm` drops `tools`, `tool_choice` and `parallel_tool_calls` when `tools == []` (`hosted_vllm/chat/transformation.py:90-95`).

### (e) Outbound fetch at runtime

- **Cost map.** On import, LiteLLM fetches the model cost map from `raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json` (override with `LITELLM_MODEL_COST_MAP_URL`). The bundled copy is used instead when any of these hold (`repos/litellm/litellm/__init__.py:413-416`; `repos/litellm/litellm/litellm_core_utils/get_model_cost_map.py:1-9,53,72-74,426-437`; `repos/litellm/litellm/litellm_core_utils/offline_mode.py:4-7`):
  - `LITELLM_LOCAL_MODEL_COST_MAP=True`
  - `LITELLM_OFFLINE_MODE=true`
  - the process runs as the `lite` or `litellm-proxy` CLI entry point (not plain `litellm`)
- **Other remote URLs** exist for the blog RSS, Anthropic beta headers and autorouter presets (`__init__.py:417-428`). Whether and when they are fetched, and whether offline mode covers them: **not verified**.
- This matters only if our harness imports the litellm SDK. The gateway's own egress is the owner's concern.

---

## 6. Known long-session weaknesses (evidence, not opinion)

- **Repetition and degeneration loops** are the dominant reported failure for GLM-5.3-Flash in agent harnesses:
  - sglang#36669 and opencode#45533 (open or unfixed)
  - vllm#54337 (fixed; triggered after about 8–20 tool turns)
  - sglang#38031 (KV-offload corruption at 100K–230K-token prompts)
  - all in §4
- **Qwen3.8-Flash-Next** failures cluster around:
  - the thinking plus `qwen3_coder` interaction (sglang#36537, fixed)
  - tool-call markup in quoted code (vllm#57541, open)
  - malformed calls deep into long sessions (colonel-otto #3, unresolved)
  - premature empty turns (HF #50) [search]
- **Runaway identical tool calls with no circuit breaker**: 220+ calls in about 25 minutes [search, https://github.com/earendil-works/pi/issues/9637]. Not verified which model.
- **Vendor statements:**
  - Z.ai says earlier GLMs, "including GLM-5", "tend to exhaust their repertoire early … then plateau" (`repos/glm-5/README.md:53`). This is about flagships, not Flash.
  - Qwen ships always-on loop guards in Qwen Code (`repos/qwen-code/packages/core/src/services/loopDetectionService.ts:76-82,150,163-167`): a halt after 5 identical tool calls, a repeated-content detector, a thought-repeat limit of 3, a file re-read window, and a stagnation counter. It also notes that DashScope once returned a server-side 400 "Repetitive tool calls detected" (`:78-80`).
- **Context length:** the native 262K limit for Qwen was validated only for startup, not with a full 262K request (`Qwen3.8-Flash-Next.yaml:399-403`).
- **No independent benchmark** measuring quality against context length or turn count for either Flash model was found. **Not verified.** Plan to measure it ourselves (checklist items 7–8).

---

## 7. Published sampling settings

- **Qwen3.8-Flash-Next**, as relayed by the SGLang cookbook ("Qwen publishes a preset per mode"; `Qwen3.8-Flash-Next.mdx:318`; the model card itself was not read):

  | Mode | temperature | top_p | top_k | min_p | presence_penalty | repetition_penalty |
  |---|---|---|---|---|---|---|
  | Thinking | 1.0 | 0.95 | 20 | 0 | 0 | 1.0 |
  | Non-thinking | 0.7 | 0.80 | 20 | 0 | 1.5 | 1.0 |

  - `generation_config.json` ships only the thinking preset, so set the non-thinking values explicitly.
  - Through LiteLLM, `top_k`, `min_p` and `repetition_penalty` travel in `extra_body` (§5c).
  - vLLM applies `generation_config` defaults by default (`repos/vllm/vllm/config/model.py:374`).
- **Qwen3.8 (flagship, not Flash-Next):**
  - Raising `presence_penalty` toward 2 "curbs runaway repetition at some risk of language mixing".
  - For agentic work, Qwen suggests budgets of 262,144 reasoning and 131,072 response tokens (`repos/sglang/docs/cookbook/autoregressive/Qwen/Qwen3.8.mdx:95`).
- **GLM-5.3-Flash:**
  - Checkpoint defaults are `temperature=1.0`, `top_p=0.95`, thinking on (`GLM-5.3-Flash.mdx:269,274`). For GLM-5.3 the cookbook calls these "informational — do not hardcode" (`GLM-5.3.mdx:287`).
  - Z.ai: keep `reasoning_effort` at the default `max` for benchmark reproduction (`repos/glm-5/README.md:112`).
- **Agent-specific settings:** no separate sampling preset for agentic or tool use was found for either model. **Not verified.**

---

## 8. Questions for the gateway owner and day-one tests

### Ask

1. **Exact backend per alias.**
   - Is it a HF repo id with revision, or a hosted API (`qwen3.8-flash` on QwenCloud, GLM on Z.ai)?
   - Which quantization (BF16, FP8, NVFP4, MXFP4) and which KV-cache dtype?
   - Which engine (vLLM or SGLang) and version or commit, on which GPUs?
2. **Engine flags.**
   - `--reasoning-parser` and `--tool-call-parser` (expected: `qwen3`/`qwen3_coder`; `glm47`/`glm47`, or `glm45` reasoning on SGLang).
   - `--enable-auto-tool-choice` and `--tool-strict-level`.
   - `--default-chat-template-kwargs` (`enable_thinking`, `preserve_thinking`, `clear_thinking`) and `--reasoning-effort-map`.
   - MTP or speculative decoding on? HiCache or KV offload on? (Both appear in the bug reports.)
3. **LiteLLM deployment.**
   - Which provider prefix (`openai/` or `hosted_vllm/`) and LiteLLM version?
   - Are `drop_params` and `merge_reasoning_content_in_choices` set?
   - Any callbacks or guardrails that rewrite messages?
   - Any `max_tokens` or `timeout` defaults in `litellm_params`?
4. **Limits.**
   - Per-key RPM/TPM and maximum concurrent requests.
   - Request timeout (thinking turns can take minutes).
   - Streaming support.
   - Maximum request body size (long sessions get big).
5. **Licence.** Has legal cleared the Qwen Community License 1.0 for internal code-review use? Is the GLM-5.3-Flash licence MIT? (Both are unverified here.)

### Test on day one (scripted, logged raw)

1. **Model info.** `GET /model/info` and `GET /v1/models` (endpoints at `repos/litellm/litellm/proxy/_types.py:597-599`). Record `max_input_tokens`, `max_output_tokens`, `litellm_provider` and `api_base` (redacted).
2. **Reasoning field.** A single-turn request with tools. Is reasoning returned as `reasoning_content`, `reasoning`, or inline `</think>` in `content`? Check both streaming and non-streaming.
3. **Thinking knobs**, per model, each request expecting 200 or 400:
   - Qwen: `chat_template_kwargs.enable_thinking=false`; `reasoning_effort` ∈ {`low`, `medium`, `xhigh`} (expect 200) and {`high`, `minimal`} (expect 400 unless an effort map is set).
   - GLM: `reasoning_effort` ∈ {`low`, `high`, `max`}, and confirm that `thinking:false` / `enable_thinking:false` do **not** disable thinking.
   - Compare reasoning token counts against `usage.completion_tokens_details` (presence not verified).
4. **Turn-shape traps.** Qwen with a system message after turn 0 should give a 400. Check GLM with `content: null` and with `content: ""` on tool-call turns.
5. **Echo-back acceptance.** Send assistant turns with `reasoning_content` (string), with `reasoning`, and with `thinking_blocks`. All three should return 200. Check that the model visibly uses the earlier reasoning (planted-fact probe), and compare prompt token counts with and without echo to confirm it reached the prompt.
6. **Parallel tool calls.** A prompt that needs 3 independent reads, with `parallel_tool_calls` true and false. Count calls, validate every `arguments` string as JSON against the schema, and check that ids are unique.
7. **30-turn soak, per model**, with real repository tools and long tool outputs (5–20K tokens each), run to about 150K and about 250K tokens of context. Log per turn:
   - `finish_reason`, and calls in `content` instead of `tool_calls`
   - invalid JSON or schema violations, unknown tool names
   - repeated identical calls, n-gram repetition in reasoning (`!!!!`, `the = the`)
   - empty content with `finish_reason=stop` or `length`
   - latency, and prompt/completion tokens
   - Run with echoed reasoning on and off.
8. **Fenced-code probe.** Ask the model to review a diff whose code contains a literal `<tool_call>` or `<function=…>` inside a fenced block. Check that no phantom tool call is produced (vllm#57541).
9. **Output ceiling.** A request with `max_completion_tokens` set high (for example 64K), then with only `max_tokens`. Confirm which is honoured, the real cap, and the behaviour when it stops inside reasoning (empty content).
10. **Concurrency and rate limits.** Ramp to N concurrent sessions and record 429s, `Retry-After`, rate-limit headers, timeouts, and whether any 200 stream carries an `error` payload (LiteLLM raises on it: `gpt_transformation.py:1088-1101`).
11. **Determinism and sampling.** Check that `temperature`, `top_p` and, through `extra_body`, `top_k`/`min_p`/`presence_penalty` are accepted, with no `UnsupportedParamsError` and no silent drop. Compare outputs at temperature 0 against the defaults.

### Harness design consequences

- Always echo `reasoning_content` as a string.
- Send `content: ""`, never `null`, on tool-call turns.
- Validate and repair tool-call JSON before it re-enters history.
- Never inject mid-conversation system messages; Qwen rejects them.
- Pick `reasoning_effort` from a per-model allowlist and never send LiteLLM `thinking`.
- Treat `finish_reason=length` with empty content, or degenerate reasoning, as a retryable failure: retry with fresh sampling or lower effort.
- Add loop guards in the style of Qwen Code, such as a halt after N identical calls and a repetition detector.
- Budget context for preserved reasoning.
