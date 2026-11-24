"""A priority token bucket in front of the gateway.

Requests, not tokens, are scarce (about 25 per minute, handoff §3.1). Every request
waits for a token; when several wait, the highest priority goes first (replies,
then re-checks, then reviews, then background work), FIFO within a priority.

On a 429 the rate is halved and everything pauses for `Retry-After` (or a backoff);
after a run of successes it creeps back up to the configured ceiling (AIMD).

The control plane's proxy holds the global limiter in production; this one is the
brain's own, for standalone runs (scenarios, the VPN scorecard) and as a second fence.
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from collections.abc import Callable

PRIORITIES: dict[str, int] = {"reply": 0, "recheck": 1, "review": 2, "background": 3}


class LimiterTimeout(TimeoutError):
    pass


class RateLimiter:
    def __init__(
        self,
        rpm: float = 25.0,
        *,
        burst: float = 2.0,
        min_rpm: float = 2.0,
        recover_after: int = 10,
        default_backoff: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rpm <= 0:
            raise ValueError("rpm must be positive")
        self.max_rpm = float(rpm)
        self.rpm = float(rpm)
        self.min_rpm = min(float(min_rpm), self.max_rpm)
        self.burst = max(1.0, float(burst))
        self.recover_after = recover_after
        self.default_backoff = default_backoff
        self._clock = clock
        self._tokens = self.burst
        self._stamp = clock()
        self._paused_until = 0.0
        self._successes = 0
        self._throttles = 0
        self._granted = 0
        self._cond = threading.Condition()
        self._queue: list[tuple[int, int]] = []
        self._seq = itertools.count()

    # -- state --------------------------------------------------------------------
    @property
    def throttles(self) -> int:
        return self._throttles

    @property
    def granted(self) -> int:
        return self._granted

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._stamp)
        self._tokens = min(self.burst, self._tokens + elapsed * self.rpm / 60.0)
        self._stamp = now

    def _wait_needed(self, now: float) -> float:
        if now < self._paused_until:
            return self._paused_until - now
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) * 60.0 / self.rpm

    # -- API ----------------------------------------------------------------------
    def acquire(self, priority: str | int = "review", timeout: float | None = None) -> float:
        """Block until this request may be sent. Returns seconds waited."""
        rank = PRIORITIES[priority] if isinstance(priority, str) else int(priority)
        me = (rank, next(self._seq))
        start = self._clock()
        deadline = None if timeout is None else start + timeout
        with self._cond:
            heapq.heappush(self._queue, me)
            try:
                while True:
                    now = self._clock()
                    self._refill(now)
                    wait = self._wait_needed(now)
                    if self._queue[0] == me and wait <= 0:
                        heapq.heappop(self._queue)
                        self._tokens -= 1.0
                        self._granted += 1
                        self._cond.notify_all()
                        return now - start
                    if deadline is not None and now >= deadline:
                        raise LimiterTimeout(f"no gateway slot within {timeout:.0f}s")
                    step = wait if self._queue[0] == me and wait > 0 else 0.25
                    if deadline is not None:
                        step = min(step, max(0.0, deadline - now))
                    self._cond.wait(timeout=max(0.005, min(step, 5.0)))
            except BaseException:
                if me in self._queue:
                    self._queue.remove(me)
                    heapq.heapify(self._queue)
                    self._cond.notify_all()
                raise

    def on_success(self) -> None:
        with self._cond:
            self._successes += 1
            if self._successes >= self.recover_after and self.rpm < self.max_rpm:
                self.rpm = min(self.max_rpm, self.rpm + 1.0)
                self._successes = 0

    def on_throttle(self, retry_after: float | None = None) -> float:
        """Record a 429. Returns the pause applied, in seconds."""
        with self._cond:
            now = self._clock()
            self._throttles += 1
            self._successes = 0
            self.rpm = max(self.min_rpm, self.rpm / 2.0)
            pause = retry_after if retry_after and retry_after > 0 else self.default_backoff
            self._paused_until = max(self._paused_until, now + pause)
            self._tokens = 0.0
            self._stamp = now
            self._cond.notify_all()
            return pause


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form: fall back to our own backoff
