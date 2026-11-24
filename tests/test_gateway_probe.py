"""The gateway probe runs end to end against the fake gateway and prints ≤ 20 lines.

The fake gateway renders every request through the real chat templates, so the
template-level rejections (Qwen's late system message and effort allow-list) show up
in the probe's summary exactly as they should on the VPN.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import chat_template as ct

from habodek.brain.llm import fake_gateway as fg

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("gateway_probe", ROOT / "probe" / "gateway_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def validator(model: str, body: dict) -> None:
    ct.render(model, body)


def plausible_model(body: dict) -> fg.Reply:
    """Answers the probe's prompts roughly as a competent model would."""
    messages = body["messages"]
    text = json.dumps(messages)
    last = messages[-1]
    tools = [t["function"]["name"] for t in body.get("tools") or []]
    limit = body.get("max_tokens") or body.get("max_completion_tokens") or 10**9
    if limit <= 100:
        return fg.Reply(content="", reasoning="1, 2, 3, 4", finish_reason="length",
                        usage={"prompt_tokens": 20, "completion_tokens": limit})
    if "finish" in tools:  # soak
        results = [m for m in messages if m["role"] == "tool"]
        system = messages[0]["content"]
        total = int(re.search(r"part_01\.py to part_(\d+)\.py", system).group(1))
        if len(results) >= total:
            found = re.findall(r"# TOKEN: (\w+)", text)
            return fg.call("finish", reasoning="all read", tokens=found)
        return fg.call("read_file", reasoning="next", path=f"part_{len(results) + 1:02d}.py")
    if "three files" in text:
        if body.get("parallel_tool_calls") is False:
            return fg.call("read_file", path="a.py")
        return fg.calls(("read_file", {"path": "a.py"}), ("read_file", {"path": "b.py"}),
                        ("read_file", {"path": "c.py"}), reasoning="independent reads")
    if "secret code word" in last.get("content", ""):
        match = re.search(r"PELICAN-\d+", json.dumps(messages[:-1]))
        return fg.say(match.group(0) if match else "I do not know.", reasoning="recall")
    if last["role"] == "tool":
        return fg.say("The first word is hello.", reasoning="read it")
    return fg.say("OK", reasoning="simple")


def test_probe_end_to_end(tmp_path, capsys):
    gateway = fg.FakeGateway(plausible_model, validator=validator,
                             headers={"x-ratelimit-remaining-requests": "24"})
    with gateway.serve() as url:
        code = probe.main([
            "--base-url", url, "--key-env", "NO_SUCH_KEY_VAR",
            "--model", "qwen=qwen3.8-flash-next", "--model", "glm=glm-5.3-flash",
            "--rpm", "6000", "--out", str(tmp_path / "out"),
            "--soak-turns", "4", "--soak-tokens", "4000",
        ])
    printed = capsys.readouterr().out.strip().splitlines()
    assert code == 0, printed
    assert len(printed) <= 20
    assert printed[-1] == f"details: {tmp_path / 'out'}"
    assert printed[-2] == "crashed: none"
    summary = "\n".join(printed)
    qwen = [line for line in printed if line.startswith("qwen ")]
    glm = [line for line in printed if line.startswith("glm ")]
    assert len(qwen) == 4 and len(glm) == 4
    # Template-level facts surface in the summary.
    assert "late_sys=400" in qwen[2] and "late_sys=200" in glm[2]
    assert "ok=low,medium,xhigh" in qwen[1] and "high:400" in qwen[1]
    assert "rej=-" in glm[1]  # GLM falls back to max silently
    assert "recall=y" in qwen[1] and "parallel=3/3" in qwen[2] and "fenced=clean" in qwen[2]
    assert "recall=4/4" in qwen[3] and "stop=finish" in qwen[3]
    assert "rl-remaining-requests=24" in summary

    results = json.loads((tmp_path / "out" / "results.json").read_text())
    assert results["models"]["qwen"]["max_input_tokens"] == 262144
    assert results["models"]["glm"]["soak"]["files_read"] == 4
    assert results["models"]["qwen"]["stream"]["fields"] == ["content", "reasoning_content"]
    raw = (tmp_path / "out" / "raw.jsonl").read_text().splitlines()
    assert len(raw) == results["rate"]["requests"] + 4  # + the unpaced GETs
    assert all("Authorization" not in line for line in raw)


def test_probe_survives_a_dead_gateway(tmp_path, capsys):
    code = probe.main(["--base-url", "http://127.0.0.1:9/v1", "--model", "qwen=x",
                       "--rpm", "6000", "--out", str(tmp_path / "o"), "--only", "info,shapes",
                       "--timeout", "2"])
    printed = capsys.readouterr().out.strip().splitlines()
    assert len(printed) <= 20 and printed[-1].startswith("details: ")
    assert code == 0  # no check crashed; every request is recorded as status 0
    assert "late_sys=ERR" in "\n".join(printed)
