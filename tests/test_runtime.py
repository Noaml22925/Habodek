"""The agent loop on the fake gateway: every guard, budget and compaction path (M4).

Every request the loop sends is rendered through the real chat template of the model
it was sent to (the fake gateway's validator), compacted histories included.
"""

from __future__ import annotations

from pathlib import Path

import chat_template as ct
import pytest

from habodek.brain.codeintel.refs import SymbolIndex
from habodek.brain.codeintel.workspace import Workspace
from habodek.brain.llm import fake_gateway as fg
from habodek.brain.llm.client import LLMClient
from habodek.brain.llm.profiles import load_profiles
from habodek.brain.runtime import compaction as cp
from habodek.brain.runtime.loop import AgentTask, LoopEngine
from habodek.brain.runtime.tool import Tool, ToolContext, code_tools
from habodek.brain.trace.writer import TraceWriter
from habodek.brain.workflow.budget import RequestBudget

QWEN, GLM = "qwen3.8-flash-next", "glm-5.3-flash"
PROFILES = load_profiles(env={})


def small_window_profiles(tmp_path: Path, window: int) -> object:
    text = (Path(__file__).resolve().parents[1] / "habodek/brain/llm/models.yaml").read_text()
    text = text.replace("context_window: 131072", f"context_window: {window}")
    path = tmp_path / "models.yaml"
    path.write_text(text)
    return load_profiles(path, env={})


CONCLUDE = Tool("conclude", "Finish with a verdict.", {
    "type": "object", "properties": {"verdict": {"type": "string", "enum": ["confirmed",
                                                                            "refuted"]},
                                     "evidence": {"type": "string"}},
    "required": ["verdict", "evidence"], "additionalProperties": False}, terminal=True,
    check=lambda args, ctx: None if ":" in args["evidence"] else
    "evidence must cite file:line.")


def check_history_invariants(model: str, body: dict) -> None:
    ct.render(PROFILES.get(model).name, body)
    open_ids: set[str] = set()
    for message in body["messages"]:
        if message["role"] == "assistant":
            open_ids = {c["id"] for c in message.get("tool_calls") or []}
        elif message["role"] == "tool":
            assert message["tool_call_id"] in open_ids, "tool result without its call"


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo/a.py").write_text("def a():\n    return b()\n")
    (tmp_path / "repo/b.py").write_text("def b():\n    return None\n")
    (tmp_path / "repo/big.py").write_text("".join(f"x_{i} = {i}\n" for i in range(400)))
    return Workspace(head=tmp_path / "repo")


def setup(ws, script, tmp_path, profiles=PROFILES, **task_kw):
    gateway = fg.FakeGateway(script, validator=check_history_invariants)
    trace = TraceWriter(tmp_path / "trace")
    client = LLMClient("http://g.invalid/v1", "k", profiles=profiles, trace=trace,
                       transport=gateway.transport(), sleep=lambda s: None)
    engine = LoopEngine(client)
    ctx = ToolContext(ws=ws, index=SymbolIndex(ws, use_jedi=False))
    task = AgentTask(role="investigator", label="investigate/H1", system="You investigate.",
                     task="Does a() return None?", tools=[*code_tools(include_git=False),
                                                          CONCLUDE], **task_kw)
    return gateway, engine, ctx, task


GOOD = fg.call("conclude", verdict="confirmed", evidence="b.py:2 returns None")


def test_happy_path(ws, tmp_path):
    gateway, engine, ctx, task = setup(ws, [
        fg.calls(("read", {"paths": ["a.py", "b.py"]}), ("outline", {"paths": ["a.py"]}),
                 reasoning="read both"),
        GOOD,
    ], tmp_path)
    result = engine.run(task, ctx)
    assert result.ok and result.output["verdict"] == "confirmed"
    assert result.turns == 2 and result.requests == 2
    assert result.files_read == ["a.py", "b.py"]
    kinds = [e["kind"] for e in engine.trace.events()]
    assert kinds.count("tool_call") == 2 and kinds[-1] == "run_end"
    # The investigator model is GLM by role; reasoning was echoed on turn 2.
    assert gateway.requests[0]["model"] == GLM
    assert gateway.requests[1]["messages"][2]["reasoning_content"] == "read both"


def test_three_invalid_calls_end_malformed(ws, tmp_path):
    _, engine, ctx, task = setup(ws, [fg.raw_call("read", '{"paths": [')] * 3, tmp_path)
    result = engine.run(task, ctx)
    assert result.status == "malformed" and result.output is None


def test_invalid_then_fixed_and_terminal_check(ws, tmp_path):
    gateway, engine, ctx, task = setup(ws, [
        fg.raw_call("read", '{"path": "a.py"}'),
        fg.call("conclude", verdict="confirmed", evidence="no citation"),
        GOOD,
    ], tmp_path)
    result = engine.run(task, ctx)
    assert result.ok
    tool_msgs = [m for m in gateway.requests[2]["messages"] if m["role"] == "tool"]
    assert "missing required field 'paths'" in tool_msgs[0]["content"]
    assert "evidence must cite file:line" in tool_msgs[1]["content"]


def test_identical_calls_return_already_done_then_checkpoint(ws, tmp_path):
    same = fg.call("read", paths=["a.py"])
    gateway, engine, ctx, task = setup(ws, [same] * 5 + [GOOD], tmp_path)
    result = engine.run(task, ctx)
    assert result.ok
    assert len(result.calls) == 1  # executed once
    last = gateway.requests[-1]["messages"]
    assert any("already done" in (m.get("content") or "") for m in last if m["role"] == "tool")
    assert "identical_calls" in result.guards
    assert any(m["role"] == "user" and "Checkpoint" in m["content"] for m in last)


def test_plain_empty_and_reasoning_only_turns_are_nudged(ws, tmp_path):
    gateway, engine, ctx, task = setup(ws, [
        fg.say("I think it returns None."), fg.say(""), fg.say("", reasoning="hmm"), GOOD,
    ], tmp_path)
    result = engine.run(task, ctx)
    assert result.ok
    assert result.guards[:3] == ["plain_text", "empty", "reasoning_only"]
    users = [m["content"] for m in gateway.requests[-1]["messages"] if m["role"] == "user"]
    assert "Do not answer in plain text" in users[1]
    assert "only reasoning" in users[3]
    # No system message after the first: nudges are user messages.
    assert all(m["role"] != "system" for m in gateway.requests[-1]["messages"][1:])


def test_no_progress_checkpoint(ws, tmp_path):
    _, engine, ctx, task = setup(ws, [fg.say("thinking...")] * 4 + [GOOD], tmp_path)
    result = engine.run(task, ctx)
    assert result.ok and "no_progress" in result.guards


def test_turn_limit_forces_conclusion_with_only_the_terminal_tool(ws, tmp_path):
    gateway, engine, ctx, task = setup(ws, [
        fg.call("read", paths=["a.py"]), fg.call("read", paths=["b.py"]),
        fg.call("grep", pattern="x"), GOOD,
    ], tmp_path, max_turns=2)
    result = engine.run(task, ctx)
    assert result.ok and result.turns == 4
    assert [t["function"]["name"] for t in gateway.requests[2]["tools"]] == ["conclude"]
    assert "conclude:the turn limit (2) is reached" in result.guards
    tool_msgs = [m for m in gateway.requests[3]["messages"] if m["role"] == "tool"]
    assert "only conclude may be called now" in tool_msgs[-1]["content"]


def test_request_budget_leaves_a_reserve_to_conclude(ws, tmp_path):
    gateway, engine, ctx, task = setup(ws, [fg.call("read", paths=["a.py"]), GOOD], tmp_path,
                                       max_requests=3)
    result = engine.run(task, ctx)
    assert result.ok and result.requests == 2
    assert "conclude:the request budget is nearly spent" in result.guards


def test_never_concluding_ends_as_budget(ws, tmp_path):
    _, engine, ctx, task = setup(ws, [fg.call("read", paths=["a.py"])] * 4, tmp_path,
                                 max_requests=3)
    result = engine.run(task, ctx)
    assert result.status == "budget" and result.requests == 3


def test_parent_budget_is_shared(ws, tmp_path):
    review = RequestBudget(2, "review")
    gateway, engine, ctx, task = setup(ws, [GOOD, GOOD, GOOD], tmp_path)
    assert engine.run(task, ctx, review).ok
    assert engine.run(task, ctx, review).ok  # concludes inside the reserve
    third = engine.run(task, ctx, review)
    assert third.status == "budget" and review.used == 2 and len(gateway.requests) == 2


def test_both_models_degenerate(ws, tmp_path):
    loop = fg.say("", finish_reason="length")
    _, engine, ctx, task = setup(ws, {GLM: [loop] * 3, QWEN: [loop] * 3}, tmp_path)
    assert engine.run(task, ctx).status == "degenerate"


def test_fallback_model_continues_the_run(ws, tmp_path):
    loop = fg.say("", reasoning="a a a a " * 80)
    gateway, engine, ctx, task = setup(ws, {
        GLM: [loop] * 3, QWEN: [fg.call("read", paths=["a.py"]), GOOD]}, tmp_path)
    result = engine.run(task, ctx)
    assert result.ok and result.model == QWEN
    assert [r["model"] for r in gateway.requests] == [GLM, GLM, GLM, QWEN, QWEN]


def test_gateway_error_ends_the_run(ws, tmp_path):
    _, engine, ctx, task = setup(ws, [fg.failure(400, "bad request")], tmp_path)
    result = engine.run(task, ctx)
    assert result.status == "error" and "400" in result.error


def test_elision_of_old_tool_outputs(ws, tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "KEEP_RECENT_TOOL_RESULTS", 1)
    profiles = small_window_profiles(tmp_path, 4000)  # 60% = 2400 tokens
    gateway, engine, ctx, task = setup(ws, [
        fg.call("read", paths=["big.py"], start=1, end=200),
        fg.call("read", paths=["big.py"], start=201, end=400),
        fg.call("read", paths=["a.py"]),
        GOOD,
    ], tmp_path, profiles=profiles)
    result = engine.run(task, ctx)
    assert result.ok
    final = gateway.requests[-1]["messages"]
    tools = [m["content"] for m in final if m["role"] == "tool"]
    assert tools[0].startswith("[elided to save context: == big.py (lines 1-200")
    assert not tools[-1].startswith("[elided")
    assert any(e["kind"] == "compaction" and e["step"] == "elide"
               for e in engine.trace.events())


def test_summary_compaction_rebuilds_history(ws, tmp_path):
    profiles = small_window_profiles(tmp_path, 2500)  # 80% = 2000 tokens
    progress = fg.call(cp.SUMMARY_TOOL_NAME, established=["b.py:2 returns None"],
                       hypotheses=["a() propagates it"], open_questions=[],
                       files_read=["big.py"], next_step="check a.py")
    gateway, engine, ctx, task = setup(ws, [
        fg.call("note", text="b.py:2 returns None"),
        fg.call("read", paths=["big.py"]),
        progress,
        fg.call("read", paths=["a.py"]),
        GOOD,
    ], tmp_path, profiles=profiles)
    result = engine.run(task, ctx)
    assert result.ok
    summary_request = gateway.requests[2]
    assert [t["function"]["name"] for t in summary_request["tools"]] == [cp.SUMMARY_TOOL_NAME]
    after = gateway.requests[3]["messages"]
    assert [m["role"] for m in after[:3]] == ["system", "user", "user"]
    assert after[1]["content"] == "Does a() return None?"  # the task is pinned
    assert "Working hypotheses (UNVERIFIED)" in after[2]["content"]
    assert "- b.py:2 returns None" in after[2]["content"]  # note kept
    assert result.notes == ["b.py:2 returns None"]


def test_summary_fallback_when_the_model_does_not_cooperate(ws, tmp_path):
    profiles = small_window_profiles(tmp_path, 2500)
    gateway, engine, ctx, task = setup(ws, [
        fg.call("note", text="b.py:2 returns None"),
        fg.call("read", paths=["big.py"]),
        fg.say("I'd rather not."),
        GOOD,
    ], tmp_path, profiles=profiles)
    result = engine.run(task, ctx)
    assert result.ok and "summary_fallback" in result.guards
    assert "b.py:2 returns None" in gateway.requests[3]["messages"][2]["content"]


@pytest.mark.parametrize("model", ct.MODELS)
def test_compacted_histories_render(model):
    messages = [
        {"role": "system", "content": "s"}, {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "reasoning_content": "r",
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "read", "arguments": '{"paths": ["a"]}'}}]},
        {"role": "tool", "tool_call_id": "1", "content": "== a (lines 1-3 of 3)\n1\tx"},
        {"role": "user", "content": "Checkpoint"},
    ]
    cp.elide_old_outputs(messages, 2, keep_recent=0)
    assert messages[3]["content"].startswith("[elided")
    ct.render(model, {"messages": messages})
    rebuilt = cp.rebuild(messages, 2, cp.render_summary({"established": ["x"]}, ["n"]))
    ct.render(model, {"messages": rebuilt})
    assert len(cp.groups(messages, 2)) == 2  # the call and its result are one group
