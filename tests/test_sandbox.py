"""Sandboxed execution (M9): env-prep, the local backend, the `run` tool and witnesses.

The tests that execute code need a user + network namespace (`unshare`); they are
skipped where the kernel does not allow one. The done-when of M9 is here:
`test_s2_confirmed_by_a_failing_test_run` (a scenario confirmed by a failing test in
the sandbox) and `test_run_cannot_open_a_network_connection`.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import httpx
import pytest
from test_review_pipeline import expected, review

from eval.scenarios import build as scenario_build
from eval.scenarios.prepare import prepare
from eval.scenarios.score import score_review
from habodek.brain.contract import RunWitness
from habodek.brain.runtime.tool import ToolContext
from habodek.brain.sandbox import envprep
from habodek.brain.sandbox.core import (
    HttpSandbox,
    LocalSandbox,
    RunLog,
    RunOutcome,
    RunRequest,
    SandboxError,
    local_available,
    recipe_for,
    sandbox_from_env,
)
from habodek.brain.sandbox.tool import check_witnesses, run_tool, witnesses_from
from habodek.brain.workflow.comment import render_witnesses
from habodek.brain.workflow.pipeline import ReviewConfig

ROOT = Path(__file__).resolve().parents[1]
GO_SANDBOX = ROOT / "agent/internal/pkg/sandbox"
KEYREPO = GO_SANDBOX / "testdata/keyrepo"
# Pinned on both sides: agent/internal/pkg/sandbox/key_test.go has the same constant.
KEYREPO_KEY = "f349a213ae58eca6ead49dabb28c2b14"

needs_ns = pytest.mark.skipif(not local_available(), reason="no user+net namespaces here")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return scenario_build.build(tmp_path_factory.mktemp("sb") / "build")


@pytest.fixture(scope="module")
def s2(built, tmp_path_factory):
    work = tmp_path_factory.mktemp("s2")
    job, ws = prepare(built, "2", work / "work")
    sandbox = LocalSandbox({"head": ws.head, "base": ws.base}, work / "cache",
                           find_links=(str(built / "index"),))
    return job, ws, sandbox


# --------------------------------------------------------------------------- env-prep


def test_envprep_is_byte_identical_to_the_go_copy():
    ours = (ROOT / "habodek/brain/sandbox/envprep.py").read_bytes()
    assert (GO_SANDBOX / "envprep.py").read_bytes() == ours, \
        "copy habodek/brain/sandbox/envprep.py to agent/internal/pkg/sandbox/"


def test_envprep_parses_as_python38():
    import ast

    ast.parse((ROOT / "habodek/brain/sandbox/envprep.py").read_text(), feature_version=(3, 8))


def test_recipe_block_is_parsed_and_prose_ignored():
    recipe = envprep.read_recipe(str(KEYREPO / ".habodek.md"))
    assert recipe == {"test": ["python -m pytest -q"],
                      "tools": ["pytest-asyncio==0.23.8 --index-url=x"], "timeout": ["300"]}
    assert envprep.parse_recipe("no block here\n```python\ntest: x\n```") == {}


def test_cache_key_is_pinned_and_tracks_dependency_files(tmp_path):
    recipe = (KEYREPO / ".habodek.md").read_text()
    assert envprep.cache_key(str(KEYREPO), recipe, "docker:python:3.12") == KEYREPO_KEY
    import shutil

    copy = tmp_path / "k"
    shutil.copytree(KEYREPO, copy)
    (copy / "pkg/__init__.py").write_text("X = 2\n")  # code is not part of the key
    assert envprep.cache_key(str(copy), recipe, "docker:python:3.12") == KEYREPO_KEY
    (copy / "requirements/ci.txt").write_text("pytest-timeout==2.3.2\n")
    assert envprep.cache_key(str(copy), recipe, "docker:python:3.12") != KEYREPO_KEY


def test_plan_drops_index_options_local_projects_and_urls(tmp_path):
    log: list[str] = []
    recipe = envprep.read_recipe(str(KEYREPO / ".habodek.md"))
    args = envprep.plan(str(KEYREPO), recipe, str(tmp_path), log)
    files = [Path(args[i + 1]) for i, a in enumerate(args) if a == "-r"]
    top = files[0].read_text()
    assert "platform-common==2.3.0" in top and "evil" not in top and "-e" not in top
    nested = [Path(line.split(None, 1)[1]) for line in top.splitlines() if line.startswith("-r ")]
    assert nested and "pytest-timeout==2.3.1" in nested[0].read_text()
    specs = [a for a in args if a != "-r" and not a.endswith(".txt")]
    assert specs == ["payments-lib==1.4.2", "pytest>=8", "pytest-asyncio==0.23.8"]
    assert any("index-url" in line for line in log) and any("-e ." in line for line in log)


def test_nested_requirement_outside_the_repo_is_dropped(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (tmp_path / "secret.txt").write_text("evil==1\n")
    (repo / "requirements.txt").write_text("-r ../secret.txt\nok==1\n")
    log: list[str] = []
    copy = envprep.sanitize_requirements(str(repo), "requirements.txt", str(tmp_path), log=log)
    assert Path(copy).read_text().split() == ["ok==1"]
    assert "outside the repository" in log[0]


def test_recipe_is_read_from_the_target_branch_first(tmp_path):
    head, base = tmp_path / "head", tmp_path / "base"
    for root, cmd in ((head, "make test-head"), (base, "make test")):
        root.mkdir()
        (root / ".habodek.md").write_text(f"```habodek-sandbox\ntest: {cmd}\n```\n")
    assert recipe_for(head, base)["test"] == ["make test"]
    (base / ".habodek.md").unlink()
    assert recipe_for(head, base)["test"] == ["make test-head"]


# --------------------------------------------------------------------------- local backend


@needs_ns
def test_s2_tests_fail_at_head_and_pass_at_base(s2):
    _, _, sandbox = s2
    base = sandbox.run(RunRequest("base", "python -m pytest -q tests/test_checkout.py"))
    head = sandbox.run(RunRequest("head", "python -m pytest -q tests/test_checkout.py"))
    assert base.exit_code == 0, base.output + base.error
    assert head.exit_code == 1 and "test_declined_cancels" in head.output
    assert base.env["key"] != head.env["key"]  # 1.4.2 vs 1.5.0: two venvs
    again = sandbox.run(RunRequest("head", "python -c 'import payments; print(payments.__file__)'"))
    assert again.env["cached"] and "/venvs/" in again.output


@needs_ns
def test_run_cannot_open_a_network_connection(s2):
    _, _, sandbox = s2
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    accepted: list[object] = []
    threading.Thread(target=lambda: accepted.append(server.accept()), daemon=True).start()
    probe = ("import socket\n"
             "for host, port in (('127.0.0.1', PORT), ('10.0.0.1', 80), ('1.1.1.1', 53)):\n"
             "    try:\n"
             "        socket.create_connection((host, port), timeout=3)\n"
             "        print('CONNECTED', host)\n"
             "    except OSError as exc:\n"
             "        print('blocked', host, exc.errno)\n"
             "try:\n"
             "    socket.getaddrinfo('pypi.org', 443)\n"
             "    print('RESOLVED')\n"
             "except OSError:\n"
             "    print('no dns')\n").replace("PORT", str(port))
    out = sandbox.run(RunRequest("head", "python probe.py", files={"probe.py": probe}))
    server.close()
    assert out.exit_code == 0, out.output
    assert "CONNECTED" not in out.output and "RESOLVED" not in out.output
    assert out.output.count("blocked") == 3 and not accepted


@needs_ns
def test_run_gets_a_cleared_environment_and_a_throwaway_copy(s2, monkeypatch):
    _, ws, sandbox = s2
    monkeypatch.setenv("HABODEK_LLM_TOKEN", "hbk_secret")
    out = sandbox.run(RunRequest("head", "env; echo x > orders/__init__.py; pwd"))
    assert "hbk_secret" not in out.output and "HABODEK" not in out.output
    assert (ws.head / "orders/__init__.py").read_text() == ""  # the worktree is untouched


@needs_ns
def test_run_times_out(s2):
    _, _, sandbox = s2
    out = sandbox.run(RunRequest("head", "sleep 30", timeout_s=5))
    assert out.timed_out and out.exit_code != 0 and out.seconds < 15


def test_files_must_stay_inside_the_copy(tmp_path):
    sandbox = LocalSandbox({"head": tmp_path}, tmp_path / "cache")
    for bad in ("../x.py", "/etc/x", "a/../../x"):
        with pytest.raises(SandboxError):
            sandbox.run(RunRequest("head", "true", files={bad: "x"}))
    with pytest.raises(SandboxError, match="unknown root"):
        sandbox.run(RunRequest("/etc", "true"))


@needs_ns
def test_failed_env_prep_is_reported_not_raised(tmp_path, built):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "requirements.txt").write_text("no-such-package==9.9\n")
    sandbox = LocalSandbox({"head": repo}, tmp_path / "cache", find_links=(str(built / "index"),))
    out = sandbox.run(RunRequest("head", "true"))
    assert out.exit_code == -1 and "env-prep failed" in out.error


# --------------------------------------------------------------------------- HTTP backend


def test_http_sandbox_posts_with_the_job_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"exit_code": 1, "seconds": 0.5, "output": "F",
                                         "env": {"key": "k", "ok": True, "cached": True}})

    sandbox = HttpSandbox("http://cp.invalid/internal/sandbox/v1", "hbk_t",
                          transport=httpx.MockTransport(handler))
    out = sandbox.run(RunRequest("head", "pytest", timeout_s=9999))
    assert out.exit_code == 1 and seen["auth"] == "Bearer hbk_t"
    assert '"timeout_s":600' in seen["body"].replace(" ", "")  # clamped


def test_http_sandbox_refusal_is_an_error():
    sandbox = HttpSandbox("http://cp.invalid/x", "t", transport=httpx.MockTransport(
        lambda r: httpx.Response(403, text="unknown token")))
    with pytest.raises(SandboxError, match="403"):
        sandbox.run(RunRequest("head", "true"))


def test_sandbox_selection_from_env(tmp_path):
    assert sandbox_from_env({}, {"head": tmp_path}, tmp_path) is None
    http = sandbox_from_env({"HABODEK_SANDBOX_URL": "http://cp/x", "HABODEK_LLM_TOKEN": "t"},
                            {"head": tmp_path}, tmp_path)
    assert isinstance(http, HttpSandbox) and http.token == "t"
    local = sandbox_from_env({"HABODEK_SANDBOX": "local"}, {"head": tmp_path}, tmp_path)
    assert isinstance(local, LocalSandbox)


# --------------------------------------------------------------------------- tool and witnesses


class _Canned:
    name = "canned"

    def run(self, request: RunRequest) -> RunOutcome:
        code = 1 if request.root == "head" else 0
        return RunOutcome(exit_code=code, seconds=0.1, output=f"output at {request.root}",
                          env={"key": "k", "ok": True, "cached": True})


def test_run_tool_logs_runs_and_witnesses_are_rebuilt_from_the_log():
    log = RunLog(_Canned(), recipe={"test": ["make check"]})
    tool = run_tool(log)
    assert "make check" in tool.description and tool.parameters["properties"]["root"]["enum"] \
        == ["head", "base"]
    ctx = ToolContext(ws=None, index=None, extra={"agent": "investigate/h1"})  # type: ignore[arg-type]
    text = tool.run({"cmd": "make check", "root": "head"}, ctx)
    assert text.startswith("run1 (head): exit 1")
    assert check_witnesses(log, [{"run": "run7", "shows": "x"}]).startswith("no such run")
    assert check_witnesses(log, [{"run": "run1", "shows": "x"}]) is None
    (w,) = witnesses_from(log, [{"run": "run1", "shows": "fails"}, {"run": "run1", "shows": "y"}])
    assert (w.exit_code, w.root, w.cmd, w.output_tail) == (1, "head", "make check",
                                                            "output at head")
    assert log.runs["run1"].agent == "investigate/h1"


def test_run_limit_is_enforced():
    log = RunLog(_Canned(), max_runs=1)
    log.run(RunRequest("head", "a"))
    with pytest.raises(SandboxError, match="limit"):
        log.run(RunRequest("head", "b"))


def test_witnesses_render_without_breaking_the_comment():
    lines = render_witnesses([RunWitness(id="run1", root="head", cmd="pytest -q", exit_code=1,
                                         output_tail="```\nE assert 'paid'\n```",
                                         shows="declined order is paid")])
    text = "\n".join(lines)
    assert "fails (exit 1)" in text and "this MR's head" in text
    assert text.count("```") == 2  # the output's own fences are neutralised


# --------------------------------------------------------------------------- end to end


def _local_runs(built, tmp_path):
    def make(ws, trace):
        sandbox = LocalSandbox({"head": ws.head, "base": ws.base}, tmp_path / "cache",
                               find_links=(str(built / "index"),))
        return RunLog(sandbox, trace)
    return make


@needs_ns
def test_s2_confirmed_by_a_failing_test_run(built, tmp_path):
    """M9 done-when: the review confirms S2 with a failing test run in the sandbox."""
    result, model, trace, _ = review(built, "2", tmp_path, config=ReviewConfig(parallel=3),
                                     runs_for=_local_runs(built, tmp_path))
    assert result.status == "ok", result.reason
    assert score_review("2", expected(built, "2"), result).passed
    (posted,) = [f for f in result.findings if f.status == "posted"]
    by_root = {w.root: w for w in posted.witnesses}
    assert by_root["head"].exit_code == 1 and "test_declined_cancels" in \
        by_root["head"].output_tail
    assert by_root["base"].exit_code == 0
    body = result.comments[0].body
    assert "Reproduced in the sandbox" in body and "fails (exit 1)" in body
    runs = [e for e in trace.events() if e["kind"] == "sandbox_run"]
    assert {e["root"] for e in runs} == {"head", "base"}
    assert (trace.dir / "runs/run1.txt").is_file()


@needs_ns
def test_a_cited_run_that_did_not_happen_is_rejected(built, tmp_path):
    result, _, trace, _ = review(built, "2", tmp_path, mode="fake_run",
                                 runs_for=_local_runs(built, tmp_path))
    assert not result.comments
    assert "no such run: run99" in trace.path.read_text()
