"""Sandboxed execution of repository code (handoff §3.6, M9).

The brain never runs repository code itself. It asks a `Sandbox` to run a command in
a throwaway copy of one workspace root, with the repository's dependencies installed
and no network. In production the sandbox is the control plane
(`HttpSandbox` → `POST /internal/sandbox/v1/run`, Docker containers); for standalone
runs (scenarios, the VPN scorecard) `LocalSandbox` does the same with a user and
network namespace (`unshare`).
"""

from habodek.brain.sandbox.core import (
    HttpSandbox,
    LocalSandbox,
    RunLog,
    RunOutcome,
    RunRequest,
    Sandbox,
    SandboxError,
    local_available,
    sandbox_from_env,
)

__all__ = ["HttpSandbox", "LocalSandbox", "RunLog", "RunOutcome", "RunRequest", "Sandbox",
           "SandboxError", "local_available", "sandbox_from_env"]
