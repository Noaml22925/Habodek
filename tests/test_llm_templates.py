"""Every message list the brain sends must render through the models' real templates.

The renderer (tests/chat_template.py) is checked against the vendored parity
fixtures first, so a pass here means something.
"""

from __future__ import annotations

import json

import chat_template as ct
import pytest

from habodek.brain.llm.profiles import load_profiles
from habodek.brain.llm.shaping import ToolSpec, shape_request

PROFILES = load_profiles(env={})

# Cases whose shapes the brain produces; the renderer must match them byte for byte.
CORE_CASES = [
    "user", "system", "multi_turn", "history_no_reasoning", "empty_reasoning", "tools",
    "tool_results", "content_and_tool_calls", "agentic", "tool_argument_values",
    "invalid_tool_arguments", "effort_low", "effort_medium", "effort_xhigh", "effort_high",
    "effort_max", "mid_system", "unicode", "special_tokens_in_text", "clear_thinking",
    "history", "history_mid_tool_loop", "preserve_thinking_true",
]


@pytest.mark.parametrize("model", ct.MODELS)
def test_renderer_matches_parity_fixtures(model):
    checked = 0
    for case in ct.fixture(model)["cases"]:
        if case["name"] not in CORE_CASES:
            continue
        checked += 1
        if "error" in case:
            with pytest.raises(ct.TemplateError):
                ct.render(model, case["request"])
        else:
            assert ct.render(model, case["request"]) == case["prompt"], case["name"]
    assert checked >= 15


READ = ToolSpec(
    "read",
    "Read files.",
    {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
     "required": ["paths"]},
)
CONCLUDE = ToolSpec(
    "conclude",
    "Finish.",
    {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]},
)


def _conversation() -> list[dict]:
    return [
        {"role": "system", "content": "You investigate one hypothesis."},
        {"role": "user", "content": "Is the missing-user return dereferenced?"},
        {"role": "assistant", "content": None, "reasoning_content": "PLAN-ALPHA: read callers",
         "tool_calls": [
             {"id": "c1", "type": "function",
              "function": {"name": "read", "arguments": json.dumps({"paths": ["a.py", "b.py"]})}},
             {"id": "c2", "type": "function",
              "function": {"name": "read", "arguments": json.dumps({"paths": ["c.py"]})}},
         ]},
        {"role": "tool", "tool_call_id": "c1", "content": "1\tdef f():\n2\t    return 1"},
        {"role": "tool", "tool_call_id": "c2", "content": "1\tx = 2"},
        {"role": "assistant", "content": "", "reasoning_content": "PLAN-BETA: check profile.py",
         "tool_calls": [{"id": "c3", "type": "function",
                         "function": {"name": "read", "arguments": "{\"paths\": [\"p.py\"]}"}}]},
        {"role": "tool", "tool_call_id": "c3", "content": "1\tuser.email"},
        # The runtime never adds a late system message, but shaping must survive one.
        {"role": "system", "content": "Budget: 3 requests left."},
    ]


@pytest.mark.parametrize("model", ct.MODELS)
def test_shaped_multi_turn_tool_conversation_renders(model):
    profile = PROFILES.get(model)
    body = shape_request(profile, PROFILES.common, _conversation(), [READ, CONCLUDE])
    prompt = ct.render(model, body)
    # Earlier reasoning is echoed into the prompt, every turn.
    assert "PLAN-ALPHA" in prompt and "PLAN-BETA" in prompt
    assert "None" not in prompt
    assert "Budget: 3 requests left." in prompt
    assert "thinking" not in body
    assert body[profile.effort.param] == profile.effort.default
    for message in body["messages"][1:]:
        assert message["role"] != "system"
    for message in body["messages"]:
        if message["role"] == "assistant":
            assert message["content"] is not None


def test_unshaped_late_system_message_is_rejected_by_qwen():
    """Negative control: the renderer does catch what shaping prevents."""
    with pytest.raises(ct.TemplateError, match="System message must be at the beginning"):
        ct.render("qwen3.8-flash-next", {"messages": _conversation()})


def test_tool_call_turns_carry_empty_string_content():
    """The `content: ""` rule. The vendored GLM template tolerates null, but the
    survey saw an engine render it as "None" and loop; we never send null."""
    for model in ct.MODELS:
        body = shape_request(PROFILES.get(model), PROFILES.common, _conversation())
        assistant = [m for m in body["messages"] if m["role"] == "assistant"]
        assert assistant and all(m["content"] == "" for m in assistant)


@pytest.mark.parametrize("model", ct.MODELS)
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max", "bogus", None])
def test_every_effort_we_might_send_renders(model, effort):
    profile = PROFILES.get(model)
    body = shape_request(profile, PROFILES.common, [{"role": "user", "content": "hi"}],
                         effort=effort)
    ct.render(model, body)
    assert body[profile.effort.param] in profile.effort.allowed


@pytest.mark.parametrize("model", ct.MODELS)
def test_leading_system_messages_are_merged(model):
    body = shape_request(PROFILES.get(model), PROFILES.common, [
        {"role": "system", "content": "A"}, {"role": "system", "content": "B"},
        {"role": "user", "content": "hi"}])
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == "A\n\nB"
    ct.render(model, body)
