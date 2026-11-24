# Survey notes behind `docs/reviewer-v2.md`

Working notes from the open-source survey of 2026-11-23/24. Each file covers one
or two projects and was written by a survey agent that cloned the project and read
its code (not its README), citing `path:line` against the commit named at the top
of the file. Nothing was installed or executed. Claims the agents could not check
are marked "not verified"; key claims the design depends on were re-checked by hand
against the source (OpenHands `send_reasoning_content` defaults and forced
`security_risk`, the Qwen3.8-Flash-Next and GLM-5.3-Flash chat templates, Qwen
Code's review skill).

`<clones>/` in the files stands for the scratch directory the repositories were
cloned into; the clones themselves are not part of this repository.

| File | Covers |
|---|---|
| [`BRIEF.md`](BRIEF.md) | The brief every survey agent worked from: constraints, method, report format |
| [`models.md`](models.md) | Qwen3.8-Flash-Next and GLM-5.3-Flash: templates, thinking controls, LiteLLM behaviour, known failures, the day-one gateway checklist |
| [`anthropic-review.md`](anthropic-review.md) | Claude Code's code-review plugin, claude-code-security-review, security-guidance — as a source of process ideas only |
| [`opencode-qwencode.md`](opencode-qwencode.md) | opencode, Qwen Code (including its review skill) |
| [`openhands.md`](openhands.md) | OpenHands and the OpenHands software-agent SDK |
| [`codex-goose.md`](codex-goose.md) | Codex CLI, Goose |
| [`swe-aider.md`](swe-aider.md) | mini-SWE-agent, SWE-agent, Aider (repo map) |
| [`pragent-hermes.md`](pragent-hermes.md) | PR-Agent (GitLab layer), Hermes Agent (reasoning replay, memory) |

These are a snapshot: every surveyed project moves weekly or faster. Re-check a
claim against the current source before building on it.
