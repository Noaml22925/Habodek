"""Shared test setup.

No test may reach the network. An audit hook (installed once, for the whole session)
fails any socket connection that is not to loopback or a Unix socket: the fake
gateway's HTTP server is on 127.0.0.1, everything else is a bug.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # for `import chat_template`

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_violations: list[str] = []


def _audit(event: str, args: tuple) -> None:
    if event not in ("socket.connect", "socket.getaddrinfo"):
        return
    if event == "socket.getaddrinfo":
        host = args[0]
        if isinstance(host, bytes):
            host = host.decode()
        if host and host not in _LOOPBACK:
            _violations.append(f"getaddrinfo {host}")
            raise ConnectionRefusedError(f"test tried to resolve {host!r}: no network in tests")
        return
    sock, address = args[0], args[1]
    if getattr(sock, "family", None) == getattr(socket, "AF_UNIX", object()):
        return
    host = address[0] if isinstance(address, tuple) else str(address)
    if host not in _LOOPBACK:
        _violations.append(f"connect {address}")
        raise ConnectionRefusedError(f"test tried to connect to {address!r}: no network in tests")


sys.addaudithook(_audit)


@pytest.fixture(autouse=True)
def _no_network():
    before = len(_violations)
    yield
    new = _violations[before:]
    assert not new, f"network access attempted: {new}"
