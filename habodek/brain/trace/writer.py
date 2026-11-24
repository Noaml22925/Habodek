"""The run's trace: one directory, one `events.jsonl`, every request and response.

Reasoning is recorded in full. Traces are how a reviewer's conclusion is audited,
and the raw LLM traffic in them is the replay cassette.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any


def new_run_id(prefix: str = "r") -> str:
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


class TraceWriter:
    def __init__(self, run_dir: Path | str, run_id: str | None = None) -> None:
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or new_run_id()
        self.path = self.dir / "events.jsonl"
        self._lock = threading.Lock()
        self._seq = 0

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            record = {"seq": self._seq, "ts": round(time.time(), 3), "kind": kind, **fields}
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            return record

    def write_json(self, name: str, data: Any) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), "utf-8")
        return path

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line]


class NullTrace(TraceWriter):
    """Discards everything; for callers that do not want a trace."""

    def __init__(self) -> None:  # noqa: D107 - deliberately no directory
        self.run_id = "untraced"
        self._seq = 0
        self._lock = threading.Lock()

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        return {"kind": kind, **fields}

    def write_json(self, name: str, data: Any) -> Path:  # pragma: no cover
        return Path("/dev/null")

    def events(self) -> list[dict[str, Any]]:
        return []
