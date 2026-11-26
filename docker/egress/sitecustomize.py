"""Loaded by every Python process of the egress test (PYTHONPATH): an audit hook that
records and refuses any network connection or name lookup that is not loopback.

The Go runner clears the brain's environment but passes PYTHONPATH through, so the
brain subprocesses the control plane starts are covered too. Violations are appended
to HABODEK_EGRESS_LOG, or to /tmp/habodek-egress.jsonl when that is not set.
"""

import json
import os
import sys
import time

_LOG = os.environ.get("HABODEK_EGRESS_LOG") or "/tmp/habodek-egress.jsonl"
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", ""}


def _loopback(host):
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    host = str(host).strip("[]").lower()
    return (host in _LOCAL_NAMES or host == "::1" or host.startswith("127.")
            or host == "0.0.0.0")


def _record(event, target):
    try:
        with open(_LOG, "a") as fh:
            fh.write(json.dumps({"ts": time.time(), "pid": os.getpid(), "event": event,
                                 "target": str(target), "argv": sys.argv[:3]}) + "\n")
    except OSError:
        pass


def _hook(event, args):
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and address and not _loopback(address[0]):
            _record(event, address)
            raise OSError(101, f"egress test: connection to {address!r} refused")
    elif event == "socket.getaddrinfo":
        if not _loopback(args[0]):
            _record(event, args[0])
            raise OSError(-2, f"egress test: lookup of {args[0]!r} refused")
    elif event == "socket.gethostbyname":
        if not _loopback(args[0]):
            _record(event, args[0])
            raise OSError(-2, f"egress test: lookup of {args[0]!r} refused")


sys.addaudithook(_hook)
