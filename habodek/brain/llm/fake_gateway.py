"""A scripted, OpenAI-compatible gateway for tests and offline runs.

No model is reachable from development, so every mechanical behaviour is exercised
against this: tool calls, reasoning, malformed arguments, empty and degenerate turns,
429 storms. A script is a list of `Reply` objects (consumed in order), a dict of such
lists keyed by model, or a callable `responder(body) -> Reply`.

An optional `validator(model, body)` runs on every chat request and turns an
exception into a 400, as the real engine does when a chat template raises. Tests pass
the real templates' renderer here, so a request shape the model would reject fails.

Use `transport()` for an in-process `httpx.MockTransport`, or `serve()` for a real
HTTP server on 127.0.0.1 (for the CLI and the stdlib-only gateway probe).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx


@dataclass
class Reply:
    content: str | None = ""
    reasoning: str = ""
    tool_calls: list[tuple[str, Any]] = field(default_factory=list)  # (name, dict | raw str)
    finish_reason: str | None = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] | None = None  # full response body override
    reasoning_field: str = "reasoning_content"
    usage: dict[str, int] | None = None
    delay: float = 0.0


def say(content: str, reasoning: str = "", **kw: Any) -> Reply:
    return Reply(content=content, reasoning=reasoning, **kw)


def call(name: str, reasoning: str = "", content: str = "", **arguments: Any) -> Reply:
    return Reply(content=content, reasoning=reasoning, tool_calls=[(name, arguments)])


def calls(*pairs: tuple[str, Any], reasoning: str = "", content: str = "") -> Reply:
    return Reply(content=content, reasoning=reasoning, tool_calls=list(pairs))


def raw_call(name: str, raw_arguments: str, reasoning: str = "") -> Reply:
    return Reply(content="", reasoning=reasoning, tool_calls=[(name, raw_arguments)])


def throttled(retry_after: float | None = 1.0) -> Reply:
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    return Reply(status=429, headers=headers,
                 raw={"error": {"message": "Rate limit exceeded", "type": "rate_limit"}})


def failure(status: int, message: str = "error") -> Reply:
    return Reply(status=status, raw={"error": {"message": message}})


Script = list[Reply] | dict[str, list[Reply]] | Callable[[dict[str, Any]], Reply]


class ScriptExhausted(AssertionError):
    pass


class FakeGateway:
    def __init__(
        self,
        script: Script | None = None,
        *,
        validator: Callable[[str, dict[str, Any]], None] | None = None,
        models: dict[str, dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.script = script if script is not None else []
        self.validator = validator
        self.models = models or {
            "qwen3.8-flash-next": {"max_input_tokens": 262144, "max_output_tokens": 65536},
            "glm-5.3-flash": {"max_input_tokens": 202752, "max_output_tokens": 131072},
        }
        self.headers = headers or {}
        self.requests: list[dict[str, Any]] = []
        self.request_headers: list[dict[str, str]] = []
        self._lock = threading.Lock()
        self._cursor: dict[str, int] = {}

    # ------------------------------------------------------------------ script
    def _next(self, body: dict[str, Any]) -> Reply:
        script = self.script
        if callable(script):
            return script(body)
        key = "*"
        queue: list[Reply]
        if isinstance(script, dict):
            key = body.get("model", "")
            if key not in script:
                raise ScriptExhausted(f"no script for model {key!r}")
            queue = script[key]
        else:
            queue = script
        index = self._cursor.get(key, 0)
        if index >= len(queue):
            raise ScriptExhausted(f"script for {key!r} exhausted after {index} replies")
        self._cursor[key] = index + 1
        return queue[index]

    @property
    def remaining(self) -> int:
        if callable(self.script):
            return 0
        if isinstance(self.script, dict):
            return sum(len(q) - self._cursor.get(k, 0) for k, q in self.script.items())
        return len(self.script) - self._cursor.get("*", 0)

    # ------------------------------------------------------------------ protocol
    def handle(
        self, method: str, path: str, headers: dict[str, str], raw_body: bytes
    ) -> tuple[int, dict[str, str], dict[str, Any] | str]:
        path = path.split("?")[0].rstrip("/")
        if method == "GET" and path.endswith("/models"):
            return 200, {}, {"object": "list", "data": [
                {"id": name, "object": "model", "owned_by": "fake"} for name in self.models]}
        if method == "GET" and path.endswith("/model/info"):
            return 200, {}, {"data": [
                {"model_name": name, "litellm_params": {"model": f"hosted_vllm/{name}"},
                 "model_info": dict(info)} for name, info in self.models.items()]}
        if method != "POST" or not path.endswith("/chat/completions"):
            return 404, {}, {"error": {"message": f"no route {method} {path}"}}
        try:
            body = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            return 400, {}, {"error": {"message": "body is not JSON"}}
        with self._lock:
            self.requests.append(body)
            self.request_headers.append({k.lower(): v for k, v in headers.items()})
            try:
                reply = self._next(body)
            except ScriptExhausted as exc:
                return 500, {}, {"error": {"message": f"fake gateway: {exc}"}}
        model = body.get("model", "")
        if self.validator is not None:
            try:
                self.validator(model, body)
            except Exception as exc:  # noqa: BLE001 - surfaced as the engine would
                return 400, {}, {"error": {"message": f"{type(exc).__name__}: {exc}",
                                           "type": "BadRequestError"}}
        if reply.delay:
            time.sleep(reply.delay)
        response_headers = {**self.headers, **reply.headers}
        if reply.raw is not None and (reply.status != 200 or reply.raw.get("choices")):
            return reply.status, response_headers, reply.raw
        if reply.status != 200:
            return reply.status, response_headers, {"error": {"message": "scripted error"}}
        completion = self.completion(model, reply, body)
        if body.get("stream"):
            response_headers["Content-Type"] = "text/event-stream"
            return 200, response_headers, self.sse(completion)
        return 200, response_headers, completion

    @staticmethod
    def sse(completion: dict[str, Any]) -> str:
        """The completion as an OpenAI-style server-sent event stream."""
        choice = completion["choices"][0]
        message = choice["message"]
        base = {k: completion[k] for k in ("id", "created", "model")}
        base["object"] = "chat.completion.chunk"
        deltas: list[dict[str, Any]] = [{"role": "assistant"}]
        for key in ("reasoning_content", "reasoning"):
            if message.get(key):
                deltas.append({key: message[key]})
        if message.get("content"):
            deltas.append({"content": message["content"]})
        for index, tool_call in enumerate(message.get("tool_calls") or []):
            deltas.append({"tool_calls": [{"index": index, **tool_call}]})
        events = [{**base, "choices": [{"index": 0, "delta": d, "finish_reason": None}]}
                  for d in deltas]
        events.append({**base, "choices": [{"index": 0, "delta": {},
                                            "finish_reason": choice["finish_reason"]}],
                       "usage": completion["usage"]})
        return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events) + \
            "data: [DONE]\n\n"

    @staticmethod
    def completion(model: str, reply: Reply, body: dict[str, Any]) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": reply.content}
        if reply.reasoning:
            message[reply.reasoning_field] = reply.reasoning
        if reply.tool_calls:
            message["tool_calls"] = [
                {
                    "id": f"call_{uuid.uuid4().hex[:10]}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": args if isinstance(args, str)
                        else json.dumps(args, ensure_ascii=False),
                    },
                }
                for name, args in reply.tool_calls
            ]
        finish = reply.finish_reason or ("tool_calls" if reply.tool_calls else "stop")
        prompt_chars = len(json.dumps(body.get("messages", []), ensure_ascii=False))
        usage = reply.usage or {
            "prompt_tokens": prompt_chars // 4,
            "completion_tokens": (len(reply.content or "") + len(reply.reasoning)) // 4 + 1,
        }
        usage.setdefault("total_tokens", usage["prompt_tokens"] + usage["completion_tokens"])
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage,
        }

    # ------------------------------------------------------------------ transports
    def transport(self) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            status, headers, payload = self.handle(
                request.method, request.url.path, dict(request.headers), request.content
            )
            if isinstance(payload, str):
                return httpx.Response(status, headers=headers, content=payload.encode())
            return httpx.Response(status, headers=headers, json=payload)

        return httpx.MockTransport(_handler)

    @contextmanager
    def serve(self, host: str = "127.0.0.1", port: int = 0) -> Iterator[str]:
        """Run as a real HTTP server; yields its base URL (ending in /v1)."""
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                status, headers, payload = gateway.handle(
                    self.command, self.path, dict(self.headers.items()), raw
                )
                data = (payload if isinstance(payload, str)
                        else json.dumps(payload, ensure_ascii=False)).encode("utf-8")
                self.send_response(status)
                if "Content-Type" not in headers:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _respond  # noqa: N815

            def log_message(self, *args: Any) -> None:  # silence
                pass

        server = ThreadingHTTPServer((host, port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://{host}:{server.server_address[1]}/v1"
        finally:
            server.shutdown()
            server.server_close()
