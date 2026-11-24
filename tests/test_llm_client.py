"""The LLM client against the scripted fake gateway.

The fake gateway renders every request through the model's real chat template, so a
request shape the model would reject fails here as a 400, as it would on the VPN.
"""

from __future__ import annotations

import json

import chat_template as ct
import httpx
import pytest

from habodek.brain.llm import fake_gateway as fg
from habodek.brain.llm.client import BudgetExhausted, GatewayError, LLMClient
from habodek.brain.llm.limiter import RateLimiter
from habodek.brain.llm.profiles import load_profiles
from habodek.brain.llm.shaping import (
    ToolSpec,
    announces_call,
    is_repetitive,
    parse_response,
)
from habodek.brain.trace.writer import TraceWriter

PROFILES = load_profiles(env={})
QWEN, GLM = "qwen3.8-flash-next", "glm-5.3-flash"

READ = ToolSpec("read", "Read files.", {
    "type": "object",
    "properties": {"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
    "required": ["paths"], "additionalProperties": False})
CONCLUDE = ToolSpec("conclude", "Finish.", {
    "type": "object", "properties": {"verdict": {"type": "string", "enum": ["yes", "no"]}},
    "required": ["verdict"]})
TOOLS = [READ, CONCLUDE]


def template_validator(model: str, body: dict) -> None:
    ct.render(PROFILES.get(model).name, body)


def make(script, tmp_path=None, limiter=None, **kw):
    gateway = fg.FakeGateway(script, validator=template_validator)
    trace = TraceWriter(tmp_path / "trace") if tmp_path else None
    client = LLMClient("http://gateway.invalid/v1", "k", profiles=PROFILES, limiter=limiter,
                       trace=trace, transport=gateway.transport(), sleep=lambda s: None, **kw)
    return gateway, client


USER = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]


def test_tool_call_turn_round_trips_into_history(tmp_path):
    gateway, client = make([
        fg.call("read", reasoning="look at both", paths=["a.py", "b.py"]),
        fg.say("done", reasoning="all read"),
    ], tmp_path)
    turn = client.complete(GLM, USER, TOOLS)
    assert turn.kind == "tool_calls" and turn.reasoning == "look at both"
    assert turn.tool_calls[0].arguments == {"paths": ["a.py", "b.py"]}
    history = [*USER, turn.to_history(),
               {"role": "tool", "tool_call_id": turn.tool_calls[0].id, "content": "ok"}]
    second = client.complete(GLM, history, TOOLS)
    assert second.kind == "answer" and second.content == "done"
    sent = gateway.requests[1]["messages"][2]
    assert sent["reasoning_content"] == "look at both" and sent["content"] == ""
    kinds = [e["kind"] for e in client.trace.events()]
    assert kinds.count("llm_request") == 2 and kinds.count("llm_response") == 2
    assert gateway.request_headers[0]["x-habodek-priority"] == "review"
    assert gateway.request_headers[0]["authorization"] == "Bearer k"


@pytest.mark.parametrize("raw,needle", [
    ('{"paths": ["a.py"', "not valid JSON"),
    ('["a.py"]', "must be a JSON object"),
    ('{"paths": []}', "at least 1"),
    ('{"paths": ["a"], "extra": 1}', "unexpected field"),
    ('{}', "missing required field 'paths'"),
])
def test_malformed_arguments_are_flagged_and_never_enter_history_raw(raw, needle):
    gateway, client = make([fg.raw_call("read", raw), fg.say("ok")])
    turn = client.complete(QWEN, USER, TOOLS)
    call = turn.tool_calls[0]
    assert not call.valid and needle in call.error
    history_args = turn.to_history()["tool_calls"][0]["function"]["arguments"]
    assert json.loads(history_args) == {}
    # The history with the repaired call still renders for both models.
    history = [*USER, turn.to_history(),
               {"role": "tool", "tool_call_id": call.id, "content": f"error: {call.error}"}]
    assert client.complete(QWEN, history, TOOLS).content == "ok"
    assert client.stats.malformed_calls == 1


def test_unknown_tool_is_an_error_not_a_crash():
    _, client = make([fg.call("delete_repo", path="/")])
    call = client.complete(QWEN, USER, TOOLS).tool_calls[0]
    assert "unknown tool 'delete_repo'" in call.error


def test_empty_and_reasoning_only_turns_are_classified():
    _, client = make([fg.say(""), fg.say("", reasoning="hmm, thinking")])
    assert client.complete(QWEN, USER, TOOLS).kind == "empty"
    assert client.complete(QWEN, USER, TOOLS).kind == "reasoning_only"


def test_degenerate_turn_is_retried_then_answered():
    gateway, client = make([
        fg.Reply(content="", reasoning="x" * 300, finish_reason="length"),
        fg.Reply(content="", reasoning="", finish_reason="length"),
        fg.say("fine"),
    ])
    turn = client.complete(QWEN, USER, TOOLS)
    assert turn.content == "fine" and turn.model == QWEN
    assert len(gateway.requests) == 3
    assert sum(client.stats.degenerate.values()) == 2


def test_repeated_degeneration_hands_the_run_to_the_other_model(tmp_path):
    loop = "the answer is " * 60
    gateway, client = make({
        QWEN: [fg.say("", reasoning=loop)] * 3,
        GLM: [fg.call("conclude", verdict="yes")],
    }, tmp_path)
    turn = client.complete(QWEN, USER, TOOLS)
    assert turn.model == GLM and turn.tool_calls[0].arguments == {"verdict": "yes"}
    assert [r["model"] for r in gateway.requests] == [QWEN, QWEN, QWEN, GLM]
    assert client.stats.fallbacks == 1 and client.stats.degenerate["repetition"] == 3
    assert any(e["kind"] == "model_fallback" for e in client.trace.events())


def test_degenerate_on_both_models_is_returned_not_looped():
    _, client = make({QWEN: [fg.say("", finish_reason="length")] * 3,
                      GLM: [fg.say("", finish_reason="length")] * 3})
    turn = client.complete(QWEN, USER, TOOLS)
    assert turn.degenerate and turn.model == GLM


def test_announced_call_without_call_is_degenerate_but_fenced_code_is_not():
    _, client = make([
        fg.say("<tool_call>\n<function=read>\n<parameter=paths>\n[\"a\"]\n"),
        fg.say("Here is the diff:\n```python\n# <tool_call><function=read>\n```\nLooks fine."),
    ])
    assert client.complete(QWEN, USER, TOOLS).kind == "answer"  # after one retry
    assert client.stats.degenerate["announced_call_without_call"] == 1


def test_429_storm_backs_off_and_recovers(tmp_path):
    limiter = RateLimiter(rpm=6000, burst=5, min_rpm=60, default_backoff=0.01)
    gateway, client = make([*[fg.throttled(0.01)] * 6, fg.say("ok")], tmp_path, limiter=limiter)
    assert client.complete(QWEN, USER, TOOLS).content == "ok"
    assert client.stats.throttled == 6 and limiter.throttles == 6
    assert limiter.rpm < 6000
    assert len(gateway.requests) == 7


def test_429_forever_gives_up():
    _, client = make([fg.throttled(None)] * 5, max_throttle_retries=3)
    with pytest.raises(GatewayError, match="429"):
        client.complete(QWEN, USER, TOOLS)


def test_server_errors_are_retried_and_400_is_not():
    _, client = make([fg.failure(502), fg.failure(503), fg.say("ok")])
    assert client.complete(QWEN, USER, TOOLS).content == "ok"
    gateway, client = make([fg.failure(400, "bad"), fg.say("never")])
    with pytest.raises(GatewayError) as info:
        client.complete(QWEN, USER, TOOLS)
    assert info.value.status == 400 and len(gateway.requests) == 1


def test_a_template_rejection_surfaces_as_400():
    """If shaping ever let a bad shape through, the validator turns it into a 400."""
    gateway = fg.FakeGateway([fg.say("x")], validator=template_validator)
    http = httpx.Client(transport=gateway.transport())
    body = {"model": QWEN, "messages": [{"role": "user", "content": "a"},
                                        {"role": "system", "content": "late"}]}
    response = http.post("http://g/v1/chat/completions", json=body)
    assert response.status_code == 400 and "beginning" in response.json()["error"]["message"]


def test_budget_is_charged_per_http_request_including_retries():
    class Budget:
        def __init__(self, n):
            self.left = n

        def charge(self, requests=1):
            if self.left < requests:
                raise BudgetExhausted("out")
            self.left -= requests

    budget = Budget(2)
    _, client = make([fg.failure(500), fg.say("ok"), fg.say("again")])
    client.complete(QWEN, USER, TOOLS, budget=budget)
    assert budget.left == 0
    with pytest.raises(BudgetExhausted):
        client.complete(QWEN, USER, TOOLS, budget=budget)


def test_reasoning_is_found_in_any_field_or_inline():
    profile = PROFILES.get(QWEN)
    def resp(message):
        return {"choices": [{"message": message, "finish_reason": "stop"}]}
    assert parse_response(profile, resp({"content": "a", "reasoning": "r1"})).reasoning == "r1"
    turn = parse_response(profile, resp({"content": "<think>r2</think>\n\nanswer"}))
    assert (turn.reasoning, turn.content, turn.reasoning_source) == ("r2", "answer",
                                                                     "inline_think")
    turn = parse_response(profile, resp({"content": "r3</think>answer"}))
    assert (turn.reasoning, turn.content) == ("r3", "answer")


def test_duplicate_or_missing_call_ids_are_made_unique():
    profile = PROFILES.get(QWEN)
    message = {"content": "", "tool_calls": [
        {"id": "x", "function": {"name": "read", "arguments": '{"paths":["a"]}'}},
        {"id": "x", "function": {"name": "read", "arguments": '{"paths":["b"]}'}},
        {"function": {"name": "read", "arguments": '{"paths":["c"]}'}}]}
    turn = parse_response(profile, {"choices": [{"message": message,
                                                 "finish_reason": "tool_calls"}]}, TOOLS)
    assert len({c.id for c in turn.tool_calls}) == 3


def test_repetition_detector():
    assert is_repetitive("a" * 250)
    assert is_repetitive("I think " + "the = the " * 30)
    assert not is_repetitive("normal reasoning about code. " * 3)
    assert not is_repetitive("-" * 120)


def test_fenced_markup_is_not_an_announced_call():
    assert announces_call("I will now <tool_call>")
    assert not announces_call("```\n<tool_call>\n```")


def test_gateway_model_alias_comes_from_env():
    profiles = load_profiles(env={"HABODEK_MODEL_QWEN": "company/qwen-flash"})
    assert profiles.get(QWEN).gateway_model == "company/qwen-flash"
    assert profiles.get("company/qwen-flash").name == QWEN
