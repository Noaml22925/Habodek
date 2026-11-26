"""The egress test's detectors catch what they must (M12).

A passing egress test is only evidence if a leak would have failed it: the audit hook
must record and refuse a non-loopback connection or lookup from any Python process it
is loaded into, the proxy sink must record a request sent through it; and inside a namespace a short
run must pass with nothing recorded (which also exercises the isolation check — it
cannot run in the pytest process, whose own audit hook forbids outbound connects).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from habodek.brain.sandbox import local_available

ROOT = Path(__file__).resolve().parents[1]
EGRESS = ROOT / "docker" / "egress"


def _python(code: str, log: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(EGRESS), "HABODEK_EGRESS_LOG": str(log)}
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


def test_audit_hook_records_and_refuses_outbound(tmp_path):
    log = tmp_path / "v.jsonl"
    code = (
        "import socket\n"
        "try:\n"
        "    socket.socket().connect(('10.1.2.3', 443)); print('LEAKED connect')\n"
        "except OSError:\n"
        "    print('refused connect')\n"
        "try:\n"
        "    socket.create_connection(('pypi.org', 443), timeout=1); print('LEAKED lookup')\n"
        "except OSError:\n"
        "    print('refused lookup')\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
        "socket.create_connection(s.getsockname()).close(); print('loopback ok')\n")
    proc = _python(code, log)
    assert "LEAKED" not in proc.stdout, proc.stdout
    assert "refused connect" in proc.stdout and "refused lookup" in proc.stdout, proc.stderr
    assert "loopback ok" in proc.stdout
    events = [json.loads(x) for x in log.read_text().splitlines()]
    assert {e["event"] for e in events} == {"socket.connect", "socket.getaddrinfo"}
    assert any("10.1.2.3" in e["target"] for e in events)


def test_the_proxy_sink_records_and_forwards_nothing():
    sys.path.insert(0, str(EGRESS))
    import egress

    url = egress.start_sink()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url}))
    with pytest.raises(urllib.request.HTTPError):
        opener.open("http://example.invalid/simple/", timeout=5)
    assert any("example.invalid" in s for s in egress.Sink.seen)


@pytest.mark.skipif(not local_available(), reason="no user+net namespaces here")
def test_inside_a_namespace_nothing_leaves_and_a_short_run_passes(tmp_path):
    proc = subprocess.run([str(EGRESS / "run.sh"), "--netns", "--scenarios", "3", "--skip-e2e",
                           "--out", str(tmp_path / "e")], capture_output=True, text=True,
                          timeout=600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout.splitlines()
    assert out[0].startswith("egress PASS") and "scorecard ok: 1/1" in proc.stdout
    assert "outbound attempts: none" in proc.stdout and len(out) <= 20
