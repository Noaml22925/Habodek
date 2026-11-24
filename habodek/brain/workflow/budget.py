"""Request budgets: a review's cap, split by stage, split by agent (handoff §3.1).

Every HTTP request to the gateway is charged to the agent's budget and, through it,
to its stage's and the review's. Whichever runs out first stops the request; the
agent must then conclude with what it has, and anything not investigated is recorded
as "not investigated: budget" — never silently dropped.
"""

from __future__ import annotations

import threading

from habodek.brain.llm.client import BudgetExhausted

# Defaults for a typical MR (≈10 files, a few hundred changed lines); configurable.
REVIEW_CAP = 300
STAGE_CAPS = {
    "understand": 10, "hypothesise": 70, "investigate": 140, "verify_a": 40, "verify_b": 30,
    "reverse_audit": 20, "judge": 6,
}
AGENT_CAPS = {
    "understand": 10, "finder": 10, "investigator": 14, "verifier_a": 10, "verifier_b": 10,
    "reverse_audit": 10, "judge": 5, "thread": 24, "thread_challenger": 6, "cartographer": 300,
}
THREAD_CAP = 30
CARTOGRAPHER_CAP = 300


class RequestBudget:
    def __init__(self, cap: int, name: str = "review", parent: RequestBudget | None = None):
        self.cap = int(cap)
        self.name = name
        self.parent = parent
        self.used = 0
        self._lock: threading.RLock = parent._lock if parent else threading.RLock()

    @property
    def remaining(self) -> int:
        own = self.cap - self.used
        return min(own, self.parent.remaining) if self.parent else own

    def child(self, name: str, cap: int) -> RequestBudget:
        return RequestBudget(cap, name, self)

    def charge(self, requests: int = 1) -> None:
        with self._lock:
            node: RequestBudget | None = self
            while node is not None:
                if node.used + requests > node.cap:
                    raise BudgetExhausted(f"request budget '{node.name}' spent "
                                          f"({node.used}/{node.cap})")
                node = node.parent
            node = self
            while node is not None:
                node.used += requests
                node = node.parent

    def path(self) -> str:
        return f"{self.parent.path()}/{self.name}" if self.parent else self.name
