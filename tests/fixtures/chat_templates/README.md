# Chat-template parity fixtures for the two gateway models

These let the brain's message shaping be tested **offline**, with no gateway: render
the messages we would send through the model's real chat template (Jinja2) and check
that it does not raise, that earlier reasoning lands inside the right think block,
and that nothing we send is a shape the template rejects (e.g. Qwen's "System message
must be at the beginning").

| File | Model, Hugging Face revision | Cases |
|---|---|---|
| `qwen3.8-flash-next.json` | `Qwen/Qwen3.8-Flash-Next` @ `de4b8e4d43b917e7706784d8bb445c9af86a3540` | 135 |
| `glm-5.3-flash.json` | `zai-org/GLM-5.3-Flash` @ `eb9eb208eb0d988989d07a6a12d0fdeb5f52574a` | 111 |

Each file holds `chat_template` (the model's template, verbatim), `config`,
`bos_token_id`, and `cases`: a `request` (OpenAI-style messages and tools) with the
`prompt` the template renders from it.

**Provenance.** Copied unmodified from SGLang
(`github.com/sgl-project/sglang`, Apache-2.0) at commit `563813c0` (2026-11-23),
`rust/sglang-processor/tests/fixtures/parity/`. SGLang's parity fixtures record the
Hugging Face revision each template was taken from (above). The templates
themselves belong to their model authors. Hugging Face is not reachable from the
development environment, which is why the SGLang copies are used.

**Caveat.** The gateway may serve a different revision or a quantised build with its
own template. The day-one gateway probe (`docs/reviewer-v2-handoff.md`, M1) checks
the live behaviour; these fixtures check our side of the contract.
