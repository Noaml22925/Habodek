# Gateway probe

One file, standard library only (Python ≥ 3.8). Copy `gateway_probe.py` to the VPN
machine and run:

```bash
export LITELLM_KEY=...        # the gateway key; never printed or logged
python3 gateway_probe.py --base-url https://<litellm-host>/v1 --key-env LITELLM_KEY \
    --model qwen=<qwen alias on the gateway> --model glm=<glm alias on the gateway>
```

It paces itself at 20 requests per minute (`--rpm`), makes about 55 requests per
model, and takes 10-25 minutes. It prints at most 20 lines; paste those back. Full
detail stays on the machine in the directory printed on the last line:
`raw.jsonl` (every request and response, key redacted) and `results.json`.

`--only info,reasoning,effort,echo,shapes,parallel,fenced,cap` skips the long soak.

What each summary field means (one line per model per group):

| Field | Meaning |
|---|---|
| `alias`, `in`, `out`, `backend` | `/v1/models` lists the alias; `/model/info` limits and LiteLLM backend |
| `reason=<status>/<field>` | where reasoning comes back: `reasoning_content`, `reasoning`, `inline_think`, `none` |
| `rtok` | `usage.completion_tokens_details.reasoning_tokens` present |
| `stream=<status>/<fields>` | delta fields seen when streaming; `ERRPAYLOAD` if a 200 stream carried an error |
| `effort ok=… rej=…` | `reasoning_effort` values accepted / rejected (with status) |
| `think_off` | `chat_template_kwargs.enable_thinking=false`: status / whether reasoning still came back |
| `echo … recall … d … alt` | earlier reasoning echoed as `reasoning_content`: accepted, recalled, prompt-token delta; `alt` = the same with the `reasoning` field |
| `late_sys` | a system message after turn 1 (Qwen's template should answer 400) |
| `null`, `empty` | a tool-call turn with `content: null` / `""` |
| `sampling` | `top_k`, `min_p`, `presence_penalty` accepted |
| `parallel=<calls>/<valid> off=<calls>` | three independent reads in one turn, with `parallel_tool_calls` on / off |
| `fenced` | tool-call markup inside a fenced diff produced no phantom call |
| `cap mt mct empty_at_cap huge` | `max_tokens` / `max_completion_tokens` honoured; content empty when the cap hits inside reasoning; status for a 10M cap |
| `soak t ctx read mal deg nocall markup dup recall p50 stop` | the long tool session: turns, peak prompt tokens (k), files read, malformed calls, degenerate turns, turns without a call, tool markup in plain content, repeated calls, recall of planted tokens, median latency, why it stopped |
| `rate` | 429s, `Retry-After` values, rate-limit headers, observed requests per minute |
