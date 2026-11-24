# Survey: mini-SWE-agent, SWE-agent, Aider

Read-only code review of cloned repos (nothing installed or executed). Clones live in
`<clones>/`.
I also read supporting libraries where they affect offline use: LiteLLM (`repos/litellm`, HEAD
`a527f1a7`, plus tag `v1.82.3`), SWE-ReX (`repos/SWE-ReX`, `5c995c36`), grep-ast (`repos/grep-ast`,
`9a2c49b0`) and tree-sitter-language-pack (`repos/tree-sitter-language-pack`, tags `v0.13.0`, `v1.27.2`).
Activity numbers come from `git log` on 2026-11-23.

---

## 1. mini-SWE-agent

**Identity.** https://github.com/SWE-agent/mini-swe-agent, commit `04d809ceab9df28f9adaed044884180159172930`,
last commit 2026-09-03. 2 commits in the last 90 days, 41 in 180 days, 14 distinct authors in the last 180 days.
Latest tag `v2.4.6` (2026-07-22); `__version__ = "2.4.6"` (`src/minisweagent/__init__.py:11`). Activity is
slowing: since July the only commits are a prompt tweak, pre-commit bumps and dependabot.

**Licence.** MIT (`LICENSE.md`, "Copyright (c) 2025 Kilian A. Lieret and Carlos E. Jimenez"). There are no
separately licensed parts in the core. The `modal`, `contree` and `portkey` extras are optional SDKs for
hosted services.

**Runtime and footprint.** Python ≥3.10, pure Python. Core dependencies (`pyproject.toml`): pyyaml, requests,
jinja2, pydantic 2, `litellm >= 1.75.5, != 1.82.7, != 1.82.8` (the comment says those two versions were
compromised), tenacity, rich, python-dotenv, typer, platformdirs, textual, prompt_toolkit, `datasets`
(HuggingFace, only imported by the SWE-bench runners `run/benchmarks/swebench.py:222`), and openai. There is
no lockfile in the repo (it has a `[dependency-groups]` section but no committed `uv.lock`). To vendor it you
would produce a wheelhouse with `pip download`. The heavy part is LiteLLM and its transitive dependencies
(tokenizers, tiktoken, etc.). No native binaries are fetched at install or at runtime by mini itself.

**Outbound calls / telemetry.**
- mini itself: **no telemetry or update check**. A grep for http/requests/telemetry/posthog/sentry only finds
  URLs in help strings and in the OpenRouter, Requesty and Portkey model classes
  (`models/openrouter_model.py:60,78`, `models/requesty_model.py:64,83`). Those run only if you pick those
  classes.
- Startup prints a banner with a URL. It is only printed, never fetched (`__init__.py:30-35`;
  `MSWEA_SILENT_STARTUP` suppresses it). It also loads `~/.config/mini-swe-agent/.env`
  (`__init__.py:26-36`).
- **LiteLLM's import-time cost-map fetch.** `import litellm` calls `get_model_cost_map(url=...)` on GitHub raw
  (v1.82.3 `litellm/__init__.py:456` → `get_model_cost_map.py:146-153`, `httpx.get(url, timeout=5)`). It falls
  back to the bundled copy on failure. Set `LITELLM_LOCAL_MODEL_COST_MAP=True` (`get_model_cost_map.py:258`)
  to skip the fetch. Current LiteLLM HEAD also honours `LITELLM_OFFLINE_MODE=true`
  (`litellm/litellm_core_utils/offline_mode.py:4-7`, `get_model_cost_map.py:72-74`), and HEAD has more
  GitHub-raw URLs (`anthropic_beta_headers_url`, `autorouter_presets_url`, `litellm/__init__.py:413-427`).
  mini does not set either variable; nothing in its repo mentions them. tiktoken is kept offline because
  LiteLLM points `TIKTOKEN_CACHE_DIR` at bundled tokenizers (`litellm_core_utils/default_encoding.py:17-30`).
- `_calculate_cost` **raises RuntimeError** when LiteLLM has no price for the model, unless
  `cost_tracking: ignore_errors` / `MSWEA_COST_TRACKING=ignore_errors` is set, or a registry JSON is supplied
  through `LITELLM_MODEL_REGISTRY_PATH` (`models/litellm_model.py:32,61-62,108-126`). A Qwen or GLM flash
  model behind our gateway will hit this, so we must configure one of those.

**LLM layer.** Everything goes through `litellm.completion(model=..., messages=..., tools=[BASH_TOOL],
**model_kwargs)` (`models/litellm_model.py:64-71`). An OpenAI-compatible gateway works with `model_kwargs:
{api_base: ..., custom_llm_provider: openai}` or a `hosted_vllm/`/`openai/` prefix (`docs/models/local_models.md`).
`mini.yaml:150-151` sets `drop_params: true`.

Two action modes:
- **Native tool calling** (default since v2): one tool, `bash(command)` (`models/utils/actions_toolcall.py:11-27`).
  Several tool calls per turn are allowed and run sequentially (`actions_toolcall.py:30-76`,
  `agents/default.py:154-157`).
- **Text mode**: regex for exactly one ```` ```mswea_bash_command ```` block
  (`models/litellm_textbased_model.py:8`, `models/utils/actions_text.py:15-40`).

Thinking handling: the assistant message is stored as `response.choices[0].message.model_dump()`
(`litellm_model.py:99`), so `reasoning_content` and `thinking_blocks` stay in history. Only the `extra` key is
stripped before resending (`litellm_model.py:76-79`), which means **reasoning_content is echoed back** on every
later turn. The only provider-specific fix is reordering Anthropic thinking blocks
(`models/utils/anthropic_utils.py:11`). Whether LiteLLM's `openai` provider forwards `reasoning_content` to the
gateway is not verified. LiteLLM HEAD `gpt_transformation.py` has no request-side stripping, and `hosted_vllm`
keeps string `reasoning_content` (`llms/hosted_vllm/chat/transformation.py:179-186`). The code base has no
Qwen- or GLM-specific code. The trajectory inspector renders `reasoning_content` (`run/utilities/inspector.py:213`).

**Headless mode.** Run
`MSWEA_CONFIGURED=1 mini -m <model> -t "<task>" -y --exit-immediately -o out.traj.json -c mini.yaml -c <overrides>`
(`run/mini.py:54-105`). Without `MSWEA_CONFIGURED`, a first-run wizard prompts for input
(`run/utilities/config.py:62-66`). `-y` selects yolo mode and `--exit-immediately` sets `confirm_exit=False`
(`run/mini.py:79-81`); `InteractiveAgent` also detects a non-tty stdin (`agents/interactive.py:97-106`). The
cleaner option is the Python API: `DefaultAgent(model, env, **cfg).run(task)`, about 10 lines
(`docs/usage/python_bindings.md`). There is no JSON event stream. Structured output is the trajectory file,
rewritten after **every** step (`agents/default.py:120-121`). Its format is
`{"info": {model_stats, config, mini_version, exit_status, submission}, "messages": [...], "trajectory_format": "mini-swe-agent-1.1"}`
(`default.py:159-180`). Every message carries an `extra` dict with the raw LiteLLM response, cost, timestamp,
returncode and raw output (`litellm_model.py:100-105`, `actions_toolcall.py:95-104`). That is enough for
observability. There is no replay CLI. `DeterministicModel` / `DeterministicToolcallModel`
(`models/test_models.py:90-178`) can feed recorded outputs back in, which is the basis for a replay.
Tool restriction: none beyond `bash`. In confirm mode `whitelist_actions` regexes apply
(`agents/interactive.py:162-163`). Exit statuses (strings, not process codes): `Submitted`, `LimitsExceeded`,
`TimeExceeded`, `RepeatedFormatError` and exception class names.

**The agent loop** (`agents/default.py:88-157`, the whole thing):
```
run(): messages=[system, instance]                              # :91-95
  while True:                                                   # :96
    try: step()  -> execute_actions(query())                    # :98, :126-128
    except FormatError: count; exit after 3 in a row            # :100-114
    except InterruptAgentFlow (Submitted/Limits): add messages  # :115-116
    except Exception: add "exit" message, re-raise              # :117-119
    finally: save(output_path)                                  # :120-121
    if messages[-1].role == "exit": break                       # :122-123
query(): check step_limit/cost_limit/wall_time, n_calls++, model.query()   # :130-152
```
The task ends when the command output's first line is `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` with return code
0. The text after that line becomes the submission (`environments/local.py:45-56`). Model API retries use
tenacity: 10 attempts, exponential backoff of 4-60 s (`models/utils/retry.py:21`), aborting on
auth/not-found/context-window errors (`litellm_model.py:50-57`). Malformed tool calls produce a templated
corrective user message. `format_error_template` separates `finish_reason=length` truncation from real format
errors (`mini.yaml:129-149`). Defaults: `step_limit: 0` (unlimited), `cost_limit: 3.0`
(`agents/default.py:26-33`, `mini.yaml:101-102`). With zero-cost local models we must set `step_limit` and
`wall_time_limit_seconds`.

**Context management.** **None.** History is strictly linear and append-only by design. The only control is
truncating the observation template: outputs of ≥10 000 characters become head 5000 + tail 5000 + `elided_chars`
(`mini.yaml:112-128`). There is no summarisation or compaction. `ContextWindowExceededError` aborts the run
(`litellm_model.py:54`).

**Tools.** Only bash, with no persistent shell: every command runs in a fresh subshell (`environments/local.py:72-92`,
which kills the process group on timeout; default timeout 30 s at `local.py:16`). The prompt teaches
`nl -ba | sed -n`, `sed -i` and heredocs (`mini.yaml:55-100`). There is no grep tool, LSP or repo map.

**Sub-agents / memory.** None. **Sandboxing:** `local` (none), `docker` (`docker run -d ... sleep 2h`, then
`docker exec`; `run_args` defaults to `["--rm"]`, so there is no network isolation unless you add
`--network none`; `environments/docker.py:30,74-99,101-138`), `singularity`, `bubblewrap` (read-only binds of
/usr, /bin, /lib and /etc, a read-write bind of cwd, and **no `--unshare-net`** by default;
`environments/extra/bubblewrap.py:38-65,86`), plus swerex/modal/contree extras.

**Code review support.** None. There are no review prompts and no GitHub/GitLab integration.

**Fit for Habodek.** (1) *As the engine:* it is technically viable and trivially vendorable. It speaks OpenAI
tool calling through LiteLLM, keeps reasoning_content, and writes a full JSON trajectory every step. But it is a
100-line bash loop with no context management, no search or navigation tools and no sub-agents. Reaching the
"Claude Code on the repo" quality bar would rest entirely on the model and a prompt we write. Embedding it
saves us almost nothing over writing the same loop in Go. **Biggest blocker:** no compaction, so long MR reviews
on large repos will overflow context and abort. (2) *As a source of ideas:* high value per line.

**Ideas worth stealing.**
- Make the exception the control flow. `Submitted`, `LimitsExceeded` and `FormatError` are exceptions that carry
  the messages to append (`exceptions.py`, `default.py:100-119`). This gives one exit path with a guaranteed
  final "exit" message.
- Save the trajectory in `finally` after every step (`default.py:120-121`), with the raw provider response and
  cost on each message. Do the same in Go for observability and replay.
- A head+tail elision template that tells the model how many characters were elided (`mini.yaml:112-128`).
- A format-error template that branches on `finish_reason` (`mini.yaml:129-149`). This matters for thinking
  models that burn the output budget before the tool call.
- Charge the cost of calls that fail to parse (`default.py:101-102`, fix #915).

---

## 2. SWE-agent

**Identity.** https://github.com/SWE-agent/SWE-agent, commit `3ea751c087f32b16e039a2233dd6eefecef325d5`,
last commit 2026-07-16. **0 commits in the last 90 days**, 24 in 180 days, 9 distinct author e-mails (7 names)
in the last 180 days. Latest tag `v1.1.0` (2025-05-22); `__version__ = "1.1.0"` (`sweagent/__init__.py:15`).
The README tells users to switch: "mini-swe-agent ... has superseded SWE-agent ... Our general recommendation
is to use mini-SWE-agent instead" (`README.md:19-24`). Recent commits are small fixes from outside
contributors. SWE-ReX, its runtime, last had a commit on 2026-03-02 (`v1.4.0`).

**Licence.** MIT (`LICENSE`); SWE-ReX is also MIT. No CLA found in `CONTRIBUTING.md`. The optional
`web_browser` tool bundle pulls Playwright and Chromium.

**Runtime and footprint.** Python ≥3.11 (`__init__.py:16,30-35`). Dependencies: datasets, numpy, pandas, rich,
ruamel.yaml, tenacity, unidiff, simple-parsing, flask, flask-cors, flask-socketio, pydantic(-settings),
`litellm>=1.44.12,!=1.82.7,!=1.82.8`, GitPython, ghapi, `swe-rex>=1.4.0`, textual and requests
(`pyproject.toml`). There is no lockfile. Vendoring is heavier than mini, and the runtime side is harder (below).

**Outbound calls / telemetry.**
- No telemetry in `sweagent/` (a grep for posthog/sentry/telemetry/analytics found nothing). It sends the header
  `User-Agent: swe-agent/<ver>` on LLM calls (`agent/models.py:715-719`).
- LiteLLM's import-time cost-map fetch: same as mini; set `LITELLM_LOCAL_MODEL_COST_MAP=True`. Cost-calculation
  failure raises `ModelConfigurationError` unless both `per_instance_cost_limit` and `total_cost_limit` are 0
  (`agent/models.py:744-754`). Missing `max_input_tokens` only logs a warning, but should be set for context
  checks (`models.py:695-703`).
- **Runtime installs inside the sandbox.** Tool bundles run `install.sh` with `check="raise"`
  (`tools/tools.py:302-308`): `pip install flake8` (`tools/windowed_edit_linting/install.sh`,
  `windowed_edit_replace`, `windowed_edit_rewrite`) and `pip install 'tree-sitter==0.21.3' tree-sitter-languages`
  (`tools/filemap/install.sh`; `tools/edit_anthropic/install.sh` adds `|| true`). Offline, the flake8 and
  filemap bundles **fail installation** unless the image already has the packages and pip points at a local
  index.
- **SWE-ReX Docker deployment.** The default env is `DockerDeploymentConfig(image="python:3.11",
  python_standalone_dir="/root")` (`environment/swe_env.py:27-28`). That builds an image that runs `apt-get`,
  `wget https://www.python.org/ftp/python/3.11.8/...` and `pip3 install swe-rex`
  (`SWE-ReX/src/swerex/deployment/docker.py:160-191`). Without the standalone dir it falls back to
  `pipx run swe-rex` (`docker.py:120-131`). It also pulls images when missing (`deployment/config.py:34`).
  Offline we need a prebuilt image that contains `swerex-remote`, plus `pull: never`.
- GitHub API (ghapi) is used only for `--problem_statement.github_url`, `GithubRepoConfig` clones
  (`environment/repo.py:126-191`) and the `open_pr` hook (`run/hooks/open_pr.py:99`).

**LLM layer.** LiteLLM library; `api_base`, `completion_kwargs` and `litellm_model_registry` are configurable
(`agent/models.py:66-150,705-733`). The parser can be `function_calling` (the default in `config/default.yaml`) or
text parsers (`thought_action`, backticks, xml, json; `tools/parsing.py`). Function calling demands **exactly one
tool call per response** (`tools/parsing.py:441-451`). Thinking is **lossy**: only `message.content`, `tool_calls`
and Anthropic `thinking_blocks` are kept (`models.py:761,774-778`). `_history_to_messages` rebuilds messages
without `reasoning_content` (`models.py:840-871`). So for Qwen and GLM, **reasoning_content is dropped** between
turns. That is acceptable for vLLM-style chat templates that drop past thinking anyway, but it is not
interleaved-thinking safe. No Qwen- or GLM-specific code.

**Headless mode.** `sweagent run --config config/default.yaml --agent.model.name <m> --agent.model.api_base <url>
--env.repo.path /checkout --env.deployment.type=local --problem_statement.path=task.md` (`run/run_single.py:1-28`).
Batch mode is `sweagent run-batch`. The output is `<id>.traj` JSON with `trajectory[]` (action, observation,
response, thought, execution_time, state, query, extra_info), `history`, `info` (exit_status, submission,
model_stats), `replay_config` and `environment` (`agent/agents.py:762-787,1220-1233`). The patch lands in
`/root/model.patch` (`agents.py:856,887`). There is no event stream, though hooks (`agent/hooks/abstract.py`,
`on_step_done`, `on_model_query` and so on) are a clean in-process event API. **Replay:** `sweagent run-replay
--traj_path x.traj` re-executes the recorded actions in a fresh environment with `ReplayModel`
(`run/run_replay.py:1-21,96-160`, `agent/models.py:464-526`). That replays actions, not LLM calls.

**The agent loop.** `run()` loops `step()` until `done` and saves the trajectory each step
(`agents.py:~1270-1290`). `step()` → `forward_with_handling(self.messages)` (`agents.py:1235-1258`). Requery on
`FormatError`, a blocklisted command (`_BlockedActionError`), a bash syntax error or a content-policy error, up to
`max_requeries=3` (`agents.py:158,1107-1150`). Every other failure (cost, context window, retry exhaustion, env
error, 3 consecutive command timeouts, `total_execution_timeout=1800`) goes through `attempt_autosubmission_after_error`,
which extracts the diff anyway (`agents.py:823-868,1150-1218`; `tools/tools.py:139-150`). LLM retries: 20 attempts
with random exponential backoff of 10-120 s (`models.py:55-63,809-834`). A blocklist refuses vim, nano, less,
`tail -f`, bare `python` and so on (`tools/tools.py:37-66`). `RetryAgent` plus `reviewer.py` runs several
attempts and has an LLM "chooser" pick the best (`agent/reviewer.py:1-3,292-375`).

**Context management: history processors.** These are chained over the full history on every query
(`agents.py:539-551`; `agent/history_processors.py`):
- `last_n_observations(n, polling)`: replaces all but the last n observations with
  `"Old environment output: (N lines omitted)"` and never removes the first (task) observation. `polling` batches
  removals to preserve prompt caching. Tags `keep_output` and `remove_output` override the rule
  (`history_processors.py:85-176`).
- `tag_tool_call_observations`: tags observations of chosen tools as `keep_output` (`:179-212`).
- `closed_window`: keeps only the latest file-viewer window per file and replaces older ones with
  "Outdated window with N lines omitted" (`:215-258`). Bug: a message that has numbered lines but no `[File:`
  header is *dropped* by the bare `continue` at `:246-247`.
- `cache_control(last_n_messages)` (the default config), `remove_regex` and `image_parsing`.
- Per-observation truncation through `max_observation_length` and `next_step_truncated_observation_template`
  (`agents.py:732-737`).

There is no LLM summarisation anywhere.

**Tools (the ACI).** Bundles under `tools/` are shell or Python scripts uploaded to `/root/tools`, documented to
the model from `config.yaml` signatures.
- `windowed`: `open path [line]`, `goto`, `scroll_up/down`, `create`, with a 100-line window, 2-line overlap and
  a `[File: p (N lines total)]` header plus "(n more lines above/below)" (`tools/windowed/install.sh`,
  `windowed/lib/windowed_file.py:150-175`).
- `windowed_edit_linting` `edit start:end`: runs flake8 before and after the edit with a curated select list
  (`F821,F822,F831,E111,E112,E113,E999,E902`, `windowed/lib/flake8_utils.py:141`). It **reverts the edit** if new
  errors appear and shows the before and after windows (`windowed_edit_linting/bin/edit:94-119`).
- `search`: `find_file`, `search_file`, `search_dir`. `search_dir` lists only files and match counts, and refuses
  past 100 files with "Please narrow your search" (`tools/search/bin/search_dir:18-32`).
- `edit_anthropic` `str_replace_editor`: clips output at 16 000 characters with a NOTE telling the model to grep
  for line numbers (`str_replace_editor:27-28,57-62`). For large `.py` files it shows a tree-sitter "filemap" that
  elides function bodies of 5 or more lines (`str_replace_editor:493-505`, `tools/filemap/bin/filemap:25-44`).
- `review_on_submit_m`: the first `submit` prints the full diff with a checklist and does not submit; a second
  `submit` actually submits (`tools/review_on_submit_m/bin/submit:33-46`, `config/default.yaml`
  `SUBMIT_REVIEW_MESSAGES`).

The shell is a persistent bash session in SWE-ReX, so `cd` persists. There is no LSP, references or call-graph
tool.

**Sub-agents / memory.** None. `RetryAgent` uses sequential attempts, not sub-agents. There is no persistent
memory. **Sandboxing:** SWE-ReX deployments `docker` (default), `local` (none), `modal`, `fargate`, `daytona` and
`remote`. Network isolation is not configured by default (not verified beyond the Docker run arguments).

**Code review support.** None for MR review. The only "review" pieces are self-review on submit and the
best-of-N chooser. The `open_pr` hook targets GitHub only.

**Fit for Habodek.** (1) *As the engine:* **no.** It is in maintenance mode and superseded by its own authors'
mini-swe-agent. Offline it needs a custom prebuilt SWE-ReX image and pre-installed tool dependencies. It drops
reasoning_content and allows only one tool call per turn. The ACI is tuned for SWE-bench Python fixes (flake8
linting, Python-only filemap). (2) *As a source of ideas:* good. It is the best-documented ACI and history-pruning
design among the three.

**Ideas worth stealing.**
- Search tools that return file+count summaries and *refuse* overly broad queries
  (`search_dir:29-32`). This fits a reviewer that must stay inside its context budget.
- Output clipping that tells the model how to get the rest ("use `grep -n` ...", `str_replace_editor:27`).
- A structural "filemap" view of big files: signatures kept, bodies elided, with line numbers
  (`filemap:25-44`).
- `last_n_observations` with `polling` to stay cache-friendly, plus keep/remove tags per tool
  (`history_processors.py:85-212`). This is a cheap first compaction layer before any summariser.
- A two-stage submit: show the full diff and a checklist and require a second confirm (`review_on_submit_m`).
  For Habodek this maps to "before posting, re-read each finding against the code and confirm".
- Autosubmit-on-error, which always salvages partial work (`agents.py:823-868`). The Habodek equivalent is to
  post the findings verified so far when we hit limits.

---

## 3. Aider

**Identity.** https://github.com/Aider-AI/aider, commit `5dc9490bb35f9729ef2c95d00a19ccd30c26339c`,
last commit 2026-05-22. **0 commits in the last 90 and 180 days**; 92 commits and 10 authors in the last 365 days.
Latest release tag `v0.86.2` (2026-02-11; `v0.86.3.dev` is the same day; `aider/__init__.py:3` says
`0.86.3.dev`). Upstream is effectively dormant: about six months with no commits, mostly from one maintainer
(paul-gauthier).

**Licence.** Apache-2.0 (`LICENSE.txt`). Contributions require an **Individual CLA**
(`CONTRIBUTING.md:37`). That only matters if we upstream; fork and vendor are fine. The tree-sitter `*-tags.scm`
query files are "adapted from the github repositories listed" in tree-sitter-language-pack, each under its own
licence (`aider/queries/tree-sitter-language-pack/README.md`), mostly MIT (not verified per file). grep-ast is
Apache-2.0.

**Runtime and footprint.** Python. There is a **fully pinned** `requirements.txt` (489 lines, generated by
uv/pip-compile from `requirements/requirements.in`), which makes vendoring easy with `pip download -r`. Key pins:
`litellm==1.82.3` (`requirements.txt:184`), `grep-ast==0.9.0` (:120), `tree-sitter-language-pack==0.13.0` (:422),
`tree-sitter==0.25.2` (:476), `networkx==3.4.2` (:217), scipy/numpy, `diskcache` (:78), `posthog==7.9.12` (:256),
`mixpanel==5.0.0` (:204), `tokenizers`, `huggingface-hub`, `sounddevice` and `pypandoc`. Native wheels: tree-sitter,
language-pack, numpy/scipy, tokenizers, sounddevice/portaudio.

**Outbound calls / telemetry.**
- **Analytics (PostHog; Mixpanel present but commented out).** The project key and host are hard-coded
  (`aider/analytics.py:55-57`, `:101-108`, with `enable_exception_autocapture=True`). It is **opt-in**: disabled
  unless `asked_opt_in` (`analytics.py:85-86,97-99`). Only about 10% of UUIDs are ever asked
  (`PERCENT=10`, `:15,119-135`). It writes `~/.aider/analytics.json` with a UUID (`:137-184`). To disable: pass
  `--analytics-disable` (permanently, `main.py:515-516`, `args.py:580`) or `--no-analytics` (`args.py:569`). **In
  headless runs with `--yes-always` the opt-in question is auto-answered "y"** (`main.py:649-651` →
  `io.py:865-866`), which turns PostHog on for the 10% of installs that get asked. **Always pass
  `--analytics-disable`.** `--analytics-posthog-host` can
  point it at a self-hosted instance.
- **Update check.** `--check-update` defaults to True (`args.py:605`). It calls
  `requests.get("https://pypi.org/pypi/aider-chat/json")` with **no timeout**, once a day
  (`versioncheck.py:64-95`, `:78`) and may offer a `pip install` self-upgrade (`versioncheck.py:29-61`). Disable
  with `--no-check-update`; also pass `--no-show-release-notes`.
- **Model metadata fetch, independent of LiteLLM.** `ModelInfoManager` downloads LiteLLM's
  `model_prices_and_context_window.json` from GitHub raw whenever `~/.aider/caches/...` is missing or older than
  24 h (`models.py:160-219`, timeout 5 s). On failure it writes `{}` and retries on later lookups. **No flag
  disables it.** Mitigation: register the models with `--model-metadata-file` (checked first, `models.py:223-226`)
  or pre-seed and touch the cache file. On top of that comes **LiteLLM's own import-time fetch**:
  `aider/llm.py:12-42` does *not* set `LITELLM_LOCAL_MODEL_COST_MAP`, so we must export it ourselves.
- **URL scraping.** `--detect-urls` defaults to True (`args.py:843`). URLs in the prompt are offered for
  `/web` scraping (`base_coder.py:964-984`), and under `--yes-always` they **are fetched** with httpx or
  Playwright. That would be a data-exfiltration path for MR descriptions. Pass `--no-detect-urls`.
- **Auto-installs.** `check_pip_install_extra` offers `pip install` for missing extras (`utils.py:298-320`), for
  example Playwright and Chromium (`scrape.py:40-66`). `/help` needs the `[help]` extra, which downloads
  HuggingFace embeddings (not verified in detail). OpenRouter OAuth onboarding triggers when no model or key is
  found (`main.py:777-816`; avoided by passing `--model`).
- **Repo map grammars: no runtime download** with the pinned versions. grep-ast just imports
  `tree_sitter_language_pack` (`grep_ast/tsl.py:1-8`), and language-pack **v0.13.0** ships compiled bindings
  inside the wheel (`tree_sitter_language_pack/__init__.py:191-245`, `import_module(".bindings.<lang>")`).
  **Caveat:** language-pack 1.x "downloads each parser on first use" (`README.md:109,121` at `v1.27.2`). Keep the
  0.13.0 pin, or prefetch with `ts-pack download`, if we reuse it.

**LLM layer.** LiteLLM library (lazy import, `drop_params=True`; `llm.py:21-45`). An OpenAI-compatible gateway
works through `--openai-api-base` with `openai/<model>` (`args.py:77`). It does **not** use native tool calling
for its main edit formats. Edits are text protocols (SEARCH/REPLACE `editblock`, `udiff`, `whole`, `patch`,
under `coders/`). `tools=` is sent only by the legacy `*_func` coders (`models.py:1006-1009`,
`coders/editblock_func_coder.py:10`). Reasoning: `reasoning_content` (or `reasoning`) is read from stream and
non-stream responses and wrapped in a private tag (`base_coder.py:1858-1863,1928-1947`,
`reasoning_tags.py:8`). It is then **removed** from the stored reply (`base_coder.py:1522,1986`,
`reasoning_tags.py:14-40`), so thinking is dropped between turns, which is fine because there is no tool loop.
`reasoning_tag` handles inline `<think>` models, and `--reasoning-effort` / `--thinking-tokens` exist
(`args.py:139-144`). Qwen appears only as old 2.5-coder/QwQ entries in `resources/model-settings.yml:784-927`
(Qwen3 entries are commented out at `:1573,1586`). No GLM or Zhipu entries.

**Headless mode.** Use
`aider --model openai/<m> --openai-api-base <url> --message-file task.md --yes-always --no-auto-commits
--no-check-update --no-show-release-notes --analytics-disable --no-detect-urls --no-pretty --no-stream
--no-gitignore [--chat-mode ask] [--read CONVENTIONS.md] file1 file2`
(`args.py:639-648,760`). `--message` runs a single `coder.run(with_message=...)` and then returns
(`main.py:1124-1134`). The process returns 0 even if the LLM call failed, since errors only go through
`io.tool_error` (`base_coder.py:1471-1489`). Output is **plain text/markdown on stdout**, with no JSON or event
schema. The chat transcript goes to `.aider.chat.history.md`, and `--llm-history-file` logs the raw LLM
traffic. The Python API (`Coder.create(...).run(msg)`) is "not officially supported". Tool restriction is not
applicable (it has no tools). `--show-repo-map` prints the repo map and exits (`args.py:687`,
`main.py:1075-1080`). That is handy for reusing only the map.

**The "agent loop".** Aider is **not an agent**. `run_one` sends the user message (`base_coder.py:924-944`).
After the reply, `send_message` parses and applies edits, auto-commits, and optionally lints and tests
(`:1590-1623`). If the reply mentions repo files that are not in the chat, if lint or test fails, or if an edit
fails to apply, it sets `reflected_message` and sends it back, up to `max_reflections = 3` (`:101,939-944,1556-1567`).
LLM errors are retried with exponential delay up to `RETRY_TIMEOUT` (`:1456-1489`). On `FinishReasonLength` it
continues through assistant prefill when the model supports it (`:1492-1504`). The model never runs commands or
greps by itself. It can only *ask* for files (`check_for_file_mentions`) or suggest shell commands. So it cannot
"follow callers across packages" or test hypotheses on its own.

**Context management.** `ChatSummary` uses the weak model to summarise the oldest half of `done_messages` when
they exceed `max_chat_history_tokens`, recursively, keeping the tail verbatim (`history.py:7-92`,
`base_coder.py:1002-1034`). Context is assembled per turn: system prompt, examples, summarised history, the repo
map, read-only files, in-chat files and the current messages (`format_messages`, `base_coder.py:1333`).

**Tools.** None in the agent sense. Its context-gathering features:
- The repo map (below).
- A tree-sitter-based linter (`linter.py`) plus flake8 for Python (`linter.py:121-162`).
- `/run`, `/test` and `/web` user commands.
- Git integration: auto-commit and `/undo` (`repo.py`).

**The repo map in depth** (`aider/repomap.py`):
1. **Tag extraction** (`get_tags_raw`, `:279-363`). Per file, `filename_to_lang` → tree-sitter parser → the
   language's `<lang>-tags.scm` query, taken from `queries/tree-sitter-language-pack/` with a fallback to
   `queries/tree-sitter-languages/` (`get_scm_fname`, `:805-829`). Every capture named `name.definition.*`
   becomes a `def` and every `name.reference.*` a `ref`, each a `Tag(rel_fname, fname, line, name, kind)`
   (`:29,318-336`). If a grammar yields only defs (C++, for example), refs are backfilled from **Pygments**
   `Token.Name` tokens (`:338-363`). Bundled query files: **31 languages** in the language-pack dir (Go, Python,
   Java, C/C++, C#, Rust, Ruby, JS, Bash and more) and **27** in the legacy dir (including TypeScript, Kotlin, PHP
   and Scala). See the Go query, `queries/tree-sitter-language-pack/go-tags.scm`: functions, methods, types,
   calls, selector calls and imports.
2. **Cache.** A `diskcache` SQLite database at `<root>/.aider.tags.cache.v4`, keyed by path and validated by
   mtime (`:35-43,217-264`), falling back to an in-memory dict on SQLite errors (`:177-215`). Note that it
   writes into the checkout.
3. **Graph and personalisation** (`get_ranked_tags`, `:365-574`). Nodes are files and there is a directed
   multi-edge *referencer → definer* per shared identifier.
   - Edge weight = `mul * sqrt(num_refs)` (`:501-514`).
   - `mul` is ×10 if the identifier is mentioned in the current message, ×10 if it is a long (≥8 characters)
     snake, kebab or camel-case name, ×0.1 if it starts with `_`, and ×0.1 if more than 5 files define it
     (`:487-499`).
   - Edges from files already in the chat are multiplied ×50 (`:508-509`).
   - Definitions with no references get a 0.1 self-loop (`:475-479`).
   - The personalisation vector gives `100/len(fnames)` to chat files, mentioned file names, and files whose
     path components match mentioned identifiers (`:381-445`).
   - Mentioned identifiers come from splitting the user message on `\W+`. Identifiers of 5 or more characters
     that equal a file stem also count as file mentions (`base_coder.py:678-707,709-722`).
4. **PageRank.** `nx.pagerank(G, weight="weight", personalization=..., dangling=...)` (`:519-531`). Then each
   node's rank is **distributed over its out-edges in proportion to weight**, accumulated per
   `(definer_file, ident)` (`:533-545`). That turns file rank into *symbol* rank. Definitions are sorted by that
   score, chat files are excluded, and files with no tags are appended by file rank (`:547-574`). Important
   files (README, Makefile and so on, `special.py`) are prepended (`:656-662`).
5. **Fitting the budget** (`get_ranked_tags_map_uncached`, `:629-706`). A binary search over how many ranked tags
   to include. It starts at `max_map_tokens/25` and accepts within 15% of the budget (`:676-703`). Token counts
   are estimated by sampling about 1% of lines (`:89-101`).
   - The budget defaults to `max_input_tokens/8`, clamped to 1k-4k (`models.py:782-789`).
   - With no files in the chat it grows to `map_tokens * map_mul_no_files` (8×), capped at context − 4096
     (`:120-132`).
   - Refresh modes `auto`, `always`, `files` and `manual`; `auto` caches when computing takes more than 1 s
     (`:576-627`).
6. **Rendering** (`to_tree`/`render_tree`, `:710-784`). grep-ast `TreeContext` prints, for each file, only the
   lines of interest (the definition lines), plus their **enclosing parent scopes** (class or func headers),
   joined with `⋮` (`grep_ast/grep_ast.py:10,108-147,208-261`). Lines are truncated to 100 characters
   (`repomap.py:782`). The result is placed as a user/assistant message pair before the files
   (`base_coder.py:750-761`). If the chat files are disjoint from the rest, it falls back to an unpersonalised
   global map (`:733-746`).

Quirk: `get_tags_raw` appends the last node of each capture group twice (`:306-310`), which inflates one ref
count per capture name. Harmless.

**Sub-agents / memory.** No sub-agents; "architect" mode chains two models, a planner and an editor
(`coders/architect_coder.py`). Memory: `--read CONVENTIONS.md`-style read-only files, and
`--restore-chat-history` from `.aider.chat.history.md` (`base_coder.py:519-522`). **Sandboxing:** none. It
edits the working tree directly and runs lint and test commands on the host. Docker images exist under `docker/`.

**Code review support.** Nothing built in: no review prompt, no MR or PR integration. "Review-like" use means
`--chat-mode ask` (no edits, `coders/ask_coder.py`) with the diff in the message and the repo map for context.
That gives a single-shot answer, no verification loop.

**Fit for Habodek.** (1) *As the engine:* **no.**
- It is a pair-programming editor, not an autonomous agent: no tool loop, no grep or read actions by the model,
  no structured output.
- It has several default-on outbound calls: update check, a metadata fetch that cannot be disabled, URL
  scraping, and pip auto-install offers.
- Upstream has been dormant for six months.

**Biggest blocker:** it cannot investigate on its own, which misses the "follow callers, test hypotheses" bar
by design.

(2) *As a source of ideas:* **the repo map is the single most stealable component in this batch.** It is about
870 lines and self-contained. Its dependencies are tree-sitter, the `.scm` tag queries, networkx PageRank and a
token counter, all vendorable, and it makes no network calls with language-pack 0.13.0.

**Ideas worth stealing.**
- **A diff-personalised repo map for review.** Run `get_ranked_tags` with chat files = files touched by the MR,
  mentioned identifiers = symbols in the diff hunks, and the ×50 chat-file edge boost. The result ranks the
  definitions the change *depends on*. Reversing the edge direction, or personalising on the MR files' defs,
  instead surfaces likely *callers*: the "who else is affected" list a reviewer needs (`repomap.py:365-574`).
  Feed the rendered map as the first context block, or expose it as a `repo_map(focus_files, idents, budget)`
  tool.
- **Distributing PageRank over edges to get per-symbol rank** (`:533-545`). This is better than file-level rank
  for picking which signatures to show.
- **Identifier-shape heuristics** (×10 long camel/snake names, ×0.1 private names and ubiquitous names) as cheap
  noise suppression (`:487-499`).
- **Binary-search packing to a token budget, with a 15% tolerance and sampled token counts** (`:629-706`,
  `:89-101`).
- **TreeContext rendering:** definition lines plus parent scopes with `⋮` elision (grep-ast). It is far denser
  than a file list and lets the model reason about structure without reading files.
- **An mtime-keyed tag cache** (`:233-264`). Per-repo caches make re-reviews after a push cheap. In Go, the
  equivalent is go-tree-sitter plus the same `.scm` files, or `go/packages` for Go repos.
- **Reflection on lint and test failures with a hard cap of 3** (`base_coder.py:939-944,1600-1623`). This maps
  to "verify finding → if the check fails, feed it back, at most N times".

---

## Summary of verdicts

1. **mini-SWE-agent:** MIT, small and easy to vendor. Native OpenAI tool calling through LiteLLM; keeps and
   echoes `reasoning_content`. No telemetry; needs `LITELLM_LOCAL_MODEL_COST_MAP=True` and
   `MSWEA_COST_TRACKING=ignore_errors`.
2. mini, continued: bash-only, with no compaction and no search or repo tools. As an engine it adds little over
   our own Go loop; the biggest blocker is context overflow on large reviews.
3. mini, ideas: exception-driven exits, a trajectory saved every step with raw responses, head/tail elision,
   and a `finish_reason`-aware format-error prompt.
4. **SWE-agent:** MIT, but in maintenance mode and officially superseded by mini. Not recommended as the engine.
5. SWE-agent, offline: needs a prebuilt SWE-ReX image (the default build runs wget/pip) and pre-installed
   flake8 and tree-sitter (bundles pip-install at runtime). It drops `reasoning_content` and allows one tool call
   per turn.
6. SWE-agent, ideas: an ACI that refuses broad searches, clipping messages that tell the model how to recover,
   a filemap view, `last_n_observations` pruning with cache-aware polling, two-stage self-review on submit, and
   autosubmit-on-error.
7. **Aider:** Apache-2.0 (with a CLA for upstreaming) and a fully pinned requirements.txt. Not an agent: no tool
   loop and no structured output. Upstream is dormant (no commits for about 6 months).
8. Aider, offline: needs `--analytics-disable --no-check-update --no-detect-urls`, a model metadata file (its own
   GitHub fetch has no switch) and `LITELLM_LOCAL_MODEL_COST_MAP=True`. Not viable as the engine.
9. **Aider's repo map is the top idea to steal.** Tree-sitter def/ref tags feed a personalised PageRank on the
   file graph, rank is distributed to symbols, the result is packed by binary search into a token budget and
   rendered with TreeContext. No runtime downloads with tree-sitter-language-pack 0.13.0; 1.x downloads
   grammars.
10. Recommendation from this batch: build our own loop, using mini's minimalism and trajectory format as the
    skeleton. Add a diff-personalised Aider-style repo map tool and SWE-agent-style bounded search and pruning.
    Embed none of these three.
