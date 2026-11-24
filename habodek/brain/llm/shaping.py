"""Turn our conversation into a request each model accepts, and its reply into a turn.

Internally the history is OpenAI chat messages, with an assistant turn's reasoning
always kept under `reasoning_content`. `shape_request` applies one model's rules
(models.yaml) on the way out; `parse_response` normalises what comes back and
classifies the turn, including the degenerate shapes flash models produce.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from habodek.brain.llm.profiles import Common, Profile
from habodek.brain.llm.schema import validate

REASONING_KEY = "reasoning_content"  # our canonical field in history
_REASONING_FIELDS = ("reasoning_content", "reasoning", "thinking")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# --------------------------------------------------------------------------- outbound


def shape_messages(
    profile: Profile, common: Common, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply one model's message rules to a copy of `messages`."""
    out: list[dict[str, Any]] = []
    for index, original in enumerate(messages):
        message = copy.deepcopy(original)
        role = message.get("role")
        if role == "system" and profile.system_messages == "first_only":
            leading = all(m.get("role") == "system" for m in messages[:index])
            if leading and out:
                # Several leading system messages become one.
                out[0]["content"] = f"{out[0]['content']}\n\n{message.get('content') or ''}"
                continue
            if not leading:
                # A late system message is a 400 on Qwen. The runtime should never
                # produce one; if it does, it is delivered as a user note instead.
                message = {"role": "user", "content": f"[harness] {message.get('content') or ''}"}
        if role == "assistant":
            reasoning = None
            for key in _REASONING_FIELDS:
                if message.get(key):
                    reasoning = message[key]
                    break
            for key in _REASONING_FIELDS:
                message.pop(key, None)
            if reasoning and profile.reasoning_echo == "every_assistant_turn":
                message[profile.reasoning_field] = reasoning
            if message.get("tool_calls"):
                if message.get("content") is None:
                    message["content"] = common.tool_call_turn_content
            elif message.get("content") is None:
                message["content"] = ""
        out.append(message)
    return out


def resolve_effort(profile: Profile, effort: str | None) -> str | None:
    if profile.effort is None:
        return None
    if effort is None or effort not in profile.effort.allowed:
        return profile.effort.default
    return effort


def shape_request(
    profile: Profile,
    common: Common,
    messages: list[dict[str, Any]],
    tools: list[ToolSpec] | None = None,
    *,
    effort: str | None = None,
    max_tokens: int | None = None,
    tool_choice: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the `/v1/chat/completions` body for `profile`."""
    body: dict[str, Any] = {
        "model": profile.gateway_model,
        "messages": shape_messages(profile, common, messages),
    }
    if tools:
        body["tools"] = [tool.to_openai() for tool in tools]
        if tool_choice:
            body["tool_choice"] = tool_choice
    resolved = resolve_effort(profile, effort)
    if resolved is not None and profile.effort is not None:
        body[profile.effort.param] = resolved
    sampling = dict(profile.sampling)
    extra_body = sampling.pop("extra_body", None) or {}
    body.update(sampling)
    body.update(extra_body)
    if profile.chat_template_kwargs:
        body["chat_template_kwargs"] = dict(profile.chat_template_kwargs)
    body["max_tokens"] = min(max_tokens or profile.max_output_tokens, profile.max_output_tokens)
    if extra:
        body.update(extra)
    for key in profile.never_send:
        body.pop(key, None)
    return body


# --------------------------------------------------------------------------- inbound


@dataclass
class ToolCall:
    id: str
    name: str
    raw_arguments: str
    arguments: dict[str, Any] | None  # None when invalid
    error: str | None = None  # why the call is invalid, phrased for the model

    @property
    def valid(self) -> bool:
        return self.error is None

    def to_history(self) -> dict[str, Any]:
        # vLLM turns an unparseable arguments string into {} when it renders the
        # template, so the model would later see a call it never made. We put the
        # validated object back, or {} for an invalid one -- its tool result then
        # says what was wrong.
        args = self.arguments if self.arguments is not None else {}
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(args, ensure_ascii=False)},
        }


@dataclass
class Turn:
    """One assistant turn, normalised."""

    model: str
    content: str
    reasoning: str
    tool_calls: list[ToolCall]
    finish_reason: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    kind: str = "answer"  # answer | tool_calls | empty | reasoning_only | degenerate
    degenerate_reason: str | None = None
    reasoning_source: str | None = None  # where reasoning came from in the response

    @property
    def degenerate(self) -> bool:
        return self.kind == "degenerate"

    def to_history(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.reasoning:
            message[REASONING_KEY] = self.reasoning
        if self.tool_calls:
            message["tool_calls"] = [call.to_history() for call in self.tool_calls]
        return message


_THINK_RE = re.compile(r"^\s*<think>(.*?)</think>\s*", re.DOTALL)
_DANGLING_THINK_RE = re.compile(r"^(.*?)</think>\s*", re.DOTALL)
_FENCE_RE = re.compile(r"```.*?(```|$)", re.DOTALL)
_TOOL_MARKUP_RE = re.compile(r"<tool_call>|<function=|<\|tool_call|\"tool_calls\"\s*:")
_CHAR_RUN_RE = re.compile(r"(.)\1{199,}", re.DOTALL)
_PHRASE_LOOP_RE = re.compile(r"(.{2,40}?)(?:\s*\1){24,}\s*$", re.DOTALL)


def _split_inline_reasoning(content: str) -> tuple[str, str]:
    match = _THINK_RE.match(content)
    if match:
        return match.group(1).strip(), content[match.end():]
    # Some engines strip the opening tag (it was in the prompt) and leave "...</think>".
    if "</think>" in content and "<think>" not in content:
        match = _DANGLING_THINK_RE.match(content)
        if match:
            return match.group(1).strip(), content[match.end():]
    return "", content


def is_repetitive(text: str) -> bool:
    if not text:
        return False
    tail = text[-4000:]
    if _CHAR_RUN_RE.search(tail):
        return True
    loop = _PHRASE_LOOP_RE.search(tail)
    # A ruler of dashes or a table border repeats too; a loop repeats words.
    return bool(loop and re.search(r"\w", loop.group(1)))


def announces_call(content: str) -> bool:
    """Tool-call markup in plain content, outside fenced code."""
    return bool(_TOOL_MARKUP_RE.search(_FENCE_RE.sub("", content)))


def parse_tool_calls(
    raw_calls: list[dict[str, Any]] | None, tools: list[ToolSpec] | None
) -> list[ToolCall]:
    by_name = {tool.name: tool for tool in tools or []}
    calls: list[ToolCall] = []
    seen: set[str] = set()
    for raw in raw_calls or []:
        fn = raw.get("function") or {}
        name = (fn.get("name") or "").strip()
        raw_args = fn.get("arguments")
        if isinstance(raw_args, dict):
            raw_args = json.dumps(raw_args, ensure_ascii=False)
        raw_args = raw_args if isinstance(raw_args, str) else ""
        call_id = raw.get("id") or ""
        if not call_id or call_id in seen:
            call_id = f"call_{uuid.uuid4().hex[:12]}"
        seen.add(call_id)
        error: str | None = None
        args: dict[str, Any] | None = None
        if not name:
            error = "the tool call has no function name"
        elif tools is not None and name not in by_name:
            error = f"unknown tool {name!r}; available: {', '.join(sorted(by_name))}"
        else:
            try:
                parsed = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError as exc:
                error = f"arguments are not valid JSON ({exc.msg} at char {exc.pos})"
            else:
                if not isinstance(parsed, dict):
                    error = f"arguments must be a JSON object, got {type(parsed).__name__}"
                else:
                    problems = validate(parsed, by_name[name].parameters) if name in by_name else []
                    if problems:
                        error = "; ".join(problems)
                    else:
                        args = parsed
        calls.append(ToolCall(call_id, name, raw_args, args, error))
    return calls


def parse_response(
    profile: Profile, response: dict[str, Any], tools: list[ToolSpec] | None = None
) -> Turn:
    """Normalise one non-streaming chat completion into a `Turn` and classify it."""
    choices = response.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    content = content or ""
    reasoning = ""
    source = None
    for key in (profile.reasoning_field, *_REASONING_FIELDS):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            reasoning, source = value, key
            break
    if not reasoning:
        inline, rest = _split_inline_reasoning(content)
        if inline:
            reasoning, content, source = inline, rest, "inline_think"
    calls = parse_tool_calls(message.get("tool_calls"), tools)
    turn = Turn(
        model=profile.name,
        content=content.strip() if not calls else content,
        reasoning=reasoning,
        tool_calls=calls,
        finish_reason=choice.get("finish_reason"),
        usage=response.get("usage") or {},
        reasoning_source=source,
    )
    classify(turn)
    return turn


def classify(turn: Turn) -> Turn:
    content = turn.content.strip()
    if turn.finish_reason == "length" and not content and not turn.tool_calls:
        turn.kind, turn.degenerate_reason = "degenerate", "length_without_answer"
    elif is_repetitive(turn.reasoning) or is_repetitive(content):
        turn.kind, turn.degenerate_reason = "degenerate", "repetition"
    elif not turn.tool_calls and announces_call(content):
        turn.kind, turn.degenerate_reason = "degenerate", "announced_call_without_call"
    elif turn.tool_calls:
        turn.kind = "tool_calls"
    elif content:
        turn.kind = "answer"
    elif turn.reasoning.strip():
        turn.kind = "reasoning_only"
    else:
        turn.kind = "empty"
    return turn
