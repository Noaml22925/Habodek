"""The brain's only way to a model: an OpenAI-compatible client with our rules.

- Shapes every request per model (shaping.py, models.yaml).
- Waits on the priority limiter; honours 429 and Retry-After; retries 5xx and
  connection errors with backoff. A 400 is not retried: it means our request is
  wrong for that model, and the trace says how.
- Classifies each turn. A degenerate turn is retried with fresh sampling, then the
  conversation is handed to the other model, and both facts are recorded.
- Charges every HTTP request to the caller's budget, retries included.
- Records every request and response, reasoning included, in the trace.
"""

from __future__ import annotations

import os
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from habodek.brain.llm.limiter import LimiterPool, RateLimiter, parse_retry_after
from habodek.brain.llm.profiles import Profiles, load_profiles
from habodek.brain.llm.shaping import ToolSpec, Turn, parse_response, shape_request
from habodek.brain.trace.writer import NullTrace, TraceWriter


class GatewayError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class BudgetExhausted(RuntimeError):
    """The caller's request budget is spent; the run must conclude."""


class Budget(Protocol):
    def charge(self, requests: int = 1) -> None:
        """Account for requests; raise BudgetExhausted when none are left."""


@dataclass
class Stats:
    requests: int = 0
    throttled: int = 0
    server_errors: int = 0
    malformed_calls: int = 0
    degenerate: Counter[str] = field(default_factory=Counter)
    fallbacks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "throttled": self.throttled,
            "server_errors": self.server_errors,
            "malformed_calls": self.malformed_calls,
            "degenerate": dict(self.degenerate),
            "fallbacks": self.fallbacks,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        profiles: Profiles | None = None,
        limiter: RateLimiter | LimiterPool | None = None,
        trace: TraceWriter | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 900.0,
        max_throttle_retries: int = 30,
        max_server_retries: int = 4,
        backoff_base: float = 2.0,
        sleep: Any = time.sleep,
    ) -> None:
        self.base_url = (base_url or os.environ.get("HABODEK_LLM_BASE_URL") or "").rstrip("/")
        if not self.base_url:
            raise ValueError("no gateway URL: pass base_url or set HABODEK_LLM_BASE_URL")
        self.api_key = api_key if api_key is not None else os.environ.get("HABODEK_LLM_TOKEN", "")
        self.profiles = profiles or load_profiles()
        self.limiter = limiter
        self.trace = trace or NullTrace()
        self.stats = Stats()
        self.max_throttle_retries = max_throttle_retries
        self.max_server_retries = max_server_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._http = httpx.Client(
            transport=transport, timeout=httpx.Timeout(timeout, connect=30.0), headers=headers
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- one request
    def _limiter_for(self, body: dict[str, Any]) -> RateLimiter | None:
        if isinstance(self.limiter, LimiterPool):
            return self.limiter.for_model(str(body.get("model", "")))
        return self.limiter

    def _url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def post(
        self,
        body: dict[str, Any],
        *,
        priority: str = "review",
        budget: Budget | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one chat completion, retrying throttles and server errors."""
        throttles = server_errors = 0
        while True:
            if budget is not None:
                budget.charge(1)
            limiter = self._limiter_for(body)
            waited = limiter.acquire(priority) if limiter else 0.0
            self.stats.requests += 1
            request_event = self.trace.event(
                "llm_request", priority=priority, waited_s=round(waited, 3),
                body=body, **(context or {}),
            )
            started = time.monotonic()
            try:
                response = self._http.post(
                    self._url(), json=body, headers={"X-Habodek-Priority": priority}
                )
            except httpx.HTTPError as exc:
                self.trace.event("llm_error", request_seq=request_event.get("seq"),
                                 error=f"{type(exc).__name__}: {exc}")
                server_errors += 1
                self.stats.server_errors += 1
                if server_errors > self.max_server_retries:
                    raise GatewayError(f"gateway unreachable: {exc}") from exc
                self._sleep(self.backoff_base ** server_errors + random.random())
                continue
            elapsed = round(time.monotonic() - started, 3)
            text = response.text
            try:
                payload = response.json()
            except ValueError:
                payload = None
            self.trace.event(
                "llm_response", request_seq=request_event.get("seq"), status=response.status_code,
                elapsed_s=elapsed, headers=_interesting_headers(response.headers),
                body=payload if payload is not None else text[:20000],
            )
            if response.status_code == 429:
                throttles += 1
                self.stats.throttled += 1
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                if limiter:
                    limiter.on_throttle(retry_after)
                else:
                    self._sleep(retry_after or self.backoff_base ** min(throttles, 6))
                if throttles > self.max_throttle_retries:
                    raise GatewayError("gateway kept answering 429", 429, text[:2000])
                continue
            if response.status_code >= 500:
                server_errors += 1
                self.stats.server_errors += 1
                if server_errors > self.max_server_retries:
                    raise GatewayError(
                        f"gateway error {response.status_code}", response.status_code, text[:2000]
                    )
                self._sleep(self.backoff_base ** server_errors + random.random())
                continue
            if response.status_code >= 400 or not isinstance(payload, dict):
                raise GatewayError(
                    f"gateway rejected the request ({response.status_code}): "
                    f"{_error_message(payload, text)}",
                    response.status_code,
                    text[:4000],
                )
            if isinstance(payload.get("error"), dict) and not payload.get("choices"):
                raise GatewayError(f"gateway error payload: {_error_message(payload, text)}",
                                   response.status_code, text[:4000])
            if limiter:
                limiter.on_success()
            usage = payload.get("usage") or {}
            self.stats.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.stats.completion_tokens += int(usage.get("completion_tokens") or 0)
            return payload

    # ---------------------------------------------------------------- one turn
    def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        *,
        priority: str = "review",
        effort: str | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
        budget: Budget | None = None,
        context: dict[str, Any] | None = None,
    ) -> Turn:
        """One assistant turn from `model`, with the degeneration policy applied.

        The returned turn's `.model` says which model produced it: after repeated
        degenerate turns the other model answers, and the caller should continue
        the run on it.
        """
        common = self.profiles.common
        profile = self.profiles.get(model)
        attempts = 0
        handed_over = False
        while True:
            body = shape_request(profile, common, messages, tools, effort=effort,
                                 max_tokens=max_tokens, tool_choice=tool_choice)
            payload = self.post(body, priority=priority, budget=budget,
                                context={"model": profile.name, **(context or {})})
            turn = parse_response(profile, payload, tools)
            self.stats.malformed_calls += sum(1 for call in turn.tool_calls if not call.valid)
            if not turn.degenerate:
                return turn
            self.stats.degenerate[turn.degenerate_reason or "unknown"] += 1
            self.trace.event("degenerate_turn", model=profile.name,
                             reason=turn.degenerate_reason, attempt=attempts + 1)
            attempts += 1
            if attempts <= common.degenerate_retries:
                continue  # fresh sampling: same request, new draw
            other = self.profiles.other(profile.name) if common.degenerate_then == "other_model" \
                else None
            if other is None or handed_over:
                return turn  # the caller sees a degenerate turn and decides
            self.stats.fallbacks += 1
            self.trace.event("model_fallback", from_model=profile.name, to_model=other.name,
                             reason=turn.degenerate_reason)
            profile, attempts, handed_over = other, 0, True


_HEADER_PREFIXES = ("x-ratelimit", "retry-after", "x-litellm", "llm_provider")


def _interesting_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower().startswith(_HEADER_PREFIXES)}


def _error_message(payload: Any, text: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:500]
        if error:
            return str(error)[:500]
        if payload.get("detail"):
            return str(payload["detail"])[:500]
    return text[:500]
