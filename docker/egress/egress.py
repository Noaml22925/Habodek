"""The egress test (handoff M12): the whole system, and nothing leaves the machine.

It runs, against a stub gateway on loopback:

1. the full scenario scorecard (`habodek-brain scorecard --scenarios 1-9`), every
   brain process with an audit hook that records and refuses any non-loopback
   connection or name lookup (`sitecustomize.py`);
2. the Go end-to-end test: webhook -> queue -> worktrees -> the real brain -> sandbox
   (where namespaces allow) -> GitLab threads -> reply -> verdict, plus the
   cartographer and memory, against a fake GitLab.

and it runs them in a network namespace with only a loopback interface (`docker run
--network none`, or `--netns` here), so nothing *can* get out. HTTP(S)_PROXY points at
a sink that records any request a process tries to send through a proxy. The test
passes only if both runs pass and the hook log and the sink are empty.

    python docker/egress/egress.py [--netns] [--runs N] [--scenarios 1-9] [--out DIR]
                                   [--e2e-bin PATH] [--skip-e2e]

Prints at most 20 lines; details go to --out (default a temporary directory).
"""

from __future__ import annotations

import argparse
import fcntl
import http.server
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1]  # the repository (or /opt/habodek/src in the image)


def loopback_up() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    flags = struct.unpack("16sH", fcntl.ioctl(s, 0x8913, struct.pack("16sH", b"lo", 0)))[1]
    fcntl.ioctl(s, 0x8914, struct.pack("16sH", b"lo", flags | 0x1 | 0x40))  # IFF_UP|RUNNING


def isolated() -> bool:
    """True when no route leaves the machine: every attempt is "network unreachable".
    A refusal or a timeout means packets went somewhere."""
    for host, port in (("1.1.1.1", 53), ("10.0.0.1", 80), ("192.168.1.1", 80)):
        try:
            socket.create_connection((host, port), timeout=2).close()
            return False
        except OSError as exc:
            if exc.errno not in (101, 99):  # ENETUNREACH, EADDRNOTAVAIL
                return False
    return True


class Sink(http.server.BaseHTTPRequestHandler):
    """A proxy that forwards nothing: every request is recorded and refused."""

    seen: list[str] = []

    def _refuse(self) -> None:
        Sink.seen.append(f"{self.command} {self.path}")
        self.send_response(403)
        self.end_headers()

    do_GET = do_POST = do_PUT = do_CONNECT = do_HEAD = do_DELETE = _refuse  # noqa: N815

    def log_message(self, *args: object) -> None:
        pass


def start_sink() -> str:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def find_e2e(explicit: str, out: Path, log: list[str]) -> Path | None:
    if explicit:
        return Path(explicit)
    for candidate in (shutil.which("habodek-e2e.test"),):
        if candidate:
            return Path(candidate)
    go = shutil.which("go")
    if go is None:
        return None
    binary = out / "habodek-e2e.test"
    env = {**os.environ, "GOFLAGS": "-mod=vendor", "GOPROXY": "off"}
    proc = subprocess.run([go, "test", "-c", "-o", str(binary), "./internal/orchestrator"],
                          cwd=SRC / "agent", env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        log.append(f"go test -c failed: {proc.stderr.strip()[-300:]}")
        return None
    return binary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--netns", action="store_true",
                   help="re-run inside a new user+network namespace (hosts, not containers)")
    p.add_argument("--scenarios", default="1-9")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--out")
    p.add_argument("--e2e-bin", default="")
    p.add_argument("--skip-e2e", action="store_true")
    a = p.parse_args(argv)
    if a.netns and os.environ.get("HABODEK_EGRESS_NETNS") != "1":
        args = [x for x in (argv if argv is not None else sys.argv[1:]) if x != "--netns"]
        env = {**os.environ, "HABODEK_EGRESS_NETNS": "1"}
        return subprocess.call(["unshare", "--user", "--map-root-user", "--net",
                                sys.executable, str(Path(__file__).resolve()), *args], env=env)
    if os.environ.get("HABODEK_EGRESS_NETNS") == "1":
        loopback_up()

    started = time.monotonic()
    out = Path(a.out or tempfile.mkdtemp(prefix="habodek-egress-"))
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if not isolated():
        print("egress FAIL: this is not an isolated network namespace (a route leaves the "
              "machine); run with --network none or --netns")
        return 2
    violations = out / "violations.jsonl"
    default_log = Path("/tmp/habodek-egress.jsonl")
    for path in (violations, default_log):
        path.unlink(missing_ok=True)
    sink = start_sink()
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join([str(HERE), str(SRC), os.environ.get("PYTHONPATH", "")]),
           "HABODEK_EGRESS_LOG": str(violations),
           "HTTP_PROXY": sink, "HTTPS_PROXY": sink, "http_proxy": sink, "https_proxy": sink,
           "ALL_PROXY": sink, "NO_PROXY": "127.0.0.1,localhost,::1",
           "no_proxy": "127.0.0.1,localhost,::1", "HABODEK_RPM": "6000",
           "HABODEK_HOME": str(out / "home"), "HABODEK_SANDBOX": "auto"}

    # The stub gateway: the scripted model on loopback, behind the real chat templates.
    built = out / "scenarios"
    gateway = subprocess.Popen([sys.executable, str(SRC / "tests" / "e2e_gateway.py"),
                                "--built", str(built)], env=env, stdout=subprocess.PIPE,
                               stderr=open(out / "gateway.log", "w"), text=True)
    ok = True
    try:
        url = gateway.stdout.readline().strip()
        if not url.startswith("http://127.0.0.1"):
            print(f"egress FAIL: the stub gateway did not start (see {out}/gateway.log)")
            return 1
        sc = subprocess.run(["habodek-brain", "scorecard", "--scenarios", a.scenarios,
                             "--runs", str(a.runs), "--build", str(built),
                             "--out", str(out / "scorecard")],
                            env={**env, "HABODEK_LLM_BASE_URL": url, "HABODEK_LLM_TOKEN": "x"},
                            capture_output=True, text=True)
        (out / "scorecard.txt").write_text(sc.stdout + sc.stderr)
        rows = [ln for ln in sc.stdout.splitlines() if ln.startswith("S")]
        bad = [ln for ln in rows if any(w in ln.split()[2:3] for w in
                                        ("MISS", "NOISY", "ERROR", "WRONG", "PART"))]
        from eval.scenarios.specs import select

        expected = len(select(a.scenarios))
        sc_ok = sc.returncode == 0 and len(rows) == expected and not bad
        ok &= sc_ok
        lines.append(f"scorecard {'ok' if sc_ok else 'FAIL'}: {len(rows) - len(bad)}/{expected} "
                     f"scenarios as expected" + (f"; {bad[0][:60]}" if bad else ""))
    finally:
        gateway.kill()
        gateway.wait()

    if a.skip_e2e:
        lines.append("e2e skipped (--skip-e2e)")
    else:
        binary = find_e2e(a.e2e_bin, out, lines)
        if binary is None:
            ok = False
            lines.append("e2e FAIL: no habodek-e2e.test binary and no Go toolchain to build it")
        else:
            e2e = subprocess.run([str(binary), "-test.run", "TestEndToEnd", "-test.count", "1",
                                  "-test.v"], cwd=SRC / "agent" / "internal" / "orchestrator",
                                 env={**env, "HABODEK_E2E": "1"}, capture_output=True, text=True,
                                 timeout=1800)
            (out / "e2e.txt").write_text(e2e.stdout + e2e.stderr)
            e2e_ok = e2e.returncode == 0 and "--- PASS: TestEndToEnd" in e2e.stdout
            ok &= e2e_ok
            sandbox = "without sandbox" if "running without the sandbox" in e2e.stdout \
                else "with sandbox"
            lines.append(f"e2e {'ok' if e2e_ok else 'FAIL'} ({sandbox})")

    attempts = []
    for path in (violations, default_log):
        if path.is_file():
            attempts += [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    if attempts or Sink.seen:
        ok = False
        lines.append(f"outbound attempts: {len(attempts)} by Python, {len(Sink.seen)} via proxy")
        lines += [f"  {x['event']} {x['target']} ({' '.join(x['argv'])[:60]})"
                  for x in attempts[:6]]
        lines += [f"  proxy: {s[:80]}" for s in Sink.seen[:4]]
    else:
        lines.append("outbound attempts: none (audit hook and proxy sink both empty)")
    (out / "attempts.json").write_text(json.dumps({"python": attempts, "proxy": Sink.seen},
                                                  indent=1))
    minutes = (time.monotonic() - started) / 60
    print(f"egress {'PASS' if ok else 'FAIL'} in {minutes:.0f} min "
          f"(network: loopback only, verified)")
    print("\n".join(lines[:17]))
    print(f"details: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
