from __future__ import annotations

import threading
import time

import pytest

from habodek.brain.llm.limiter import LimiterTimeout, RateLimiter, parse_retry_after


def test_burst_then_rate():
    limiter = RateLimiter(rpm=600, burst=3)  # 10 per second
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    elapsed = time.monotonic() - start
    assert 0.15 <= elapsed < 1.0  # 3 free, then two more at 0.1 s each


def test_higher_priority_goes_first():
    limiter = RateLimiter(rpm=1200, burst=1)  # one token per 50 ms
    limiter.acquire()  # drain
    order: list[str] = []
    lock = threading.Lock()

    def worker(priority: str) -> None:
        limiter.acquire(priority)
        with lock:
            order.append(priority)

    threads = [threading.Thread(target=worker, args=(p,))
               for p in ["background", "review", "review", "recheck", "reply"]]
    for thread in threads:
        thread.start()
        time.sleep(0.005)  # all queued before the next token arrives
    for thread in threads:
        thread.join(5)
    assert order == ["reply", "recheck", "review", "review", "background"]


def test_throttle_pauses_and_halves_then_recovers():
    limiter = RateLimiter(rpm=1200, burst=1, min_rpm=100, recover_after=2)
    limiter.on_throttle(0.2)
    assert limiter.rpm == 600
    start = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - start >= 0.19
    for _ in range(4):
        limiter.on_success()
    assert limiter.rpm == 602


def test_rate_never_drops_below_floor():
    limiter = RateLimiter(rpm=25, min_rpm=5)
    for _ in range(10):
        limiter.on_throttle(0.0)
    assert limiter.rpm == 5


def test_timeout_leaves_queue_clean():
    limiter = RateLimiter(rpm=1, burst=1)
    limiter.acquire()
    with pytest.raises(LimiterTimeout):
        limiter.acquire(timeout=0.05)
    assert limiter._queue == []


def test_retry_after_parsing():
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") is None
