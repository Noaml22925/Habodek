"""Render OpenAI-style requests through a model's real chat template, offline.

This is how vLLM/SGLang turn a `/v1/chat/completions` body into a prompt: tool-call
`arguments` strings are parsed into objects, `reasoning_effort` and
`chat_template_kwargs` become template variables, and `raise_exception` in the
template becomes an error (the gateway answers it with a 400).

Used by the tests to check that every message list the brain would send renders
without error and puts earlier reasoning where the model expects it. It is not a
byte-exact copy of either engine; `test_chat_template_renderer` measures how close
it gets on the vendored parity fixtures.
"""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any

import jinja2
from jinja2.sandbox import ImmutableSandboxedEnvironment

FIXTURES = Path(__file__).parent / "fixtures" / "chat_templates"
MODELS = ("qwen3.8-flash-next", "glm-5.3-flash")


class TemplateError(ValueError):
    """The template refused the request (the gateway would answer 400)."""


def _raise(message: str) -> None:
    raise TemplateError(message)


def _tojson(value: Any, ensure_ascii: bool = False, indent: int | None = None,
            separators: Any = None, sort_keys: bool = False) -> str:
    return json.dumps(value, ensure_ascii=ensure_ascii, indent=indent,
                      separators=separators, sort_keys=sort_keys)


@cache
def fixture(model: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{model}.json").read_text(encoding="utf-8"))


@cache
def _template(model: str) -> jinja2.Template:
    env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True,
                                        extensions=["jinja2.ext.loopcontrols"])
    env.filters["tojson"] = _tojson
    env.globals["raise_exception"] = _raise
    return env.from_string(fixture(model)["chat_template"])


def _prepare_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = copy.deepcopy(messages)
    for message in out:
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    fn["arguments"] = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    pass  # engines pass unparseable strings through
    return out


def render(model: str, request: dict[str, Any], add_generation_prompt: bool = True) -> str:
    """Render `request` (an OpenAI chat body) with `model`'s template.

    Raises `TemplateError` where the template raises.
    """
    variables: dict[str, Any] = {
        "messages": _prepare_messages(request["messages"]),
        "add_generation_prompt": add_generation_prompt,
        "bos_token": "",
        "eos_token": "",
    }
    if request.get("tools"):
        tools = copy.deepcopy(request["tools"])
        for tool in tools:
            fn = tool.get("function")
            if isinstance(fn, dict):
                fn.setdefault("parameters", None)  # as the engines' request models do
        variables["tools"] = tools
    if request.get("reasoning_effort") is not None:
        variables["reasoning_effort"] = request["reasoning_effort"]
    for key, value in (request.get("chat_template_kwargs") or {}).items():
        variables.setdefault(key, value)
    try:
        return _template(model).render(**variables)
    except TemplateError:
        raise
    except (jinja2.TemplateError, TypeError, AttributeError) as exc:  # pragma: no cover
        raise TemplateError(f"{type(exc).__name__}: {exc}") from exc
