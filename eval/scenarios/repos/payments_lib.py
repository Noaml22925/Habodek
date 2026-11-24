"""Source of the `payments-lib` library: version 1.4.2 and version 1.5.0.

The 1.5.0 change is the one scenario 2 is about: `PaymentClient.charge()` stops
raising `PaymentDeclined` and returns `ChargeResult(status="declined")` instead.
"""

from __future__ import annotations


def _pyproject(version: str) -> str:
    return f'''
[project]
name = "payments-lib"
version = "{version}"
description = "Client for the payment provider."
requires-python = ">=3.10"
dependencies = []
'''


FILES_1_4_2: dict[str, str] = {
    "pyproject.toml": _pyproject("1.4.2"),
    "README.md": '''
# payments-lib

`PaymentClient` wraps the payment provider: charge, refund, retrieve.
Transport is pluggable; tests use `payments.transport.FakeTransport`.
''',
    "CHANGELOG.md": '''
# Changelog

## 1.4.2
- Retry charges on gateway timeouts with the same idempotency key.

## 1.4.1
- `refund()` accepts partial amounts.

## 1.4.0
- `ChargeResult.succeeded`.
''',
    "payments/__init__.py": '''
"""payments-lib: a client for the payment provider."""

from payments.client import PaymentClient
from payments.errors import GatewayTimeout, PaymentDeclined, PaymentError
from payments.models import ChargeResult, Refund

__version__ = "1.4.2"
__all__ = ["ChargeResult", "GatewayTimeout", "PaymentClient", "PaymentDeclined",
           "PaymentError", "Refund"]
''',
    "payments/errors.py": '''
"""Errors raised by the client."""


class PaymentError(Exception):
    """Base class."""


class PaymentDeclined(PaymentError):
    """The provider declined the charge (insufficient funds, fraud check, ...)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GatewayTimeout(PaymentError):
    """The provider did not answer in time. Safe to retry with the same idempotency key."""
''',
    "payments/models.py": '''
"""Results returned by the client."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChargeResult:
    charge_id: str
    status: str  # "succeeded"
    amount_cents: int
    currency: str

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


@dataclass(frozen=True)
class Refund:
    refund_id: str
    charge_id: str
    amount_cents: int
''',
    "payments/transport.py": '''
"""Transports: how requests reach the provider."""

from __future__ import annotations

from typing import Any, Protocol


class Transport(Protocol):
    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class FakeTransport:
    """Scripted responses for tests: `FakeTransport([{"status": "succeeded", ...}])`."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append((path, payload))
        return self.responses.pop(0)
''',
    "payments/retry.py": '''
"""Retrying idempotent calls."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from payments.errors import GatewayTimeout

T = TypeVar("T")


def with_retries(fn: Callable[[], T], attempts: int = 3, delay: float = 0.2) -> T:
    for attempt in range(attempts):
        try:
            return fn()
        except GatewayTimeout:
            if attempt == attempts - 1:
                raise
            time.sleep(delay * (2 ** attempt))
    raise AssertionError("unreachable")
''',
    "payments/client.py": '''
"""The payment client."""

from __future__ import annotations

import uuid

from payments.errors import GatewayTimeout, PaymentDeclined
from payments.models import ChargeResult, Refund
from payments.retry import with_retries
from payments.transport import Transport


class PaymentClient:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def charge(self, order_id: str, amount_cents: int, currency: str,
               card_token: str) -> ChargeResult:
        """Charge a card.

        Raises PaymentDeclined when the provider declines the charge, and
        GatewayTimeout when it does not answer after retries.
        """
        key = f"order-{order_id}-{uuid.uuid5(uuid.NAMESPACE_URL, order_id).hex[:8]}"
        payload = {"amount": amount_cents, "currency": currency, "source": card_token,
                   "idempotency_key": key}
        response = with_retries(lambda: self._transport.post("/charges", payload))
        if response.get("status") == "timeout":
            raise GatewayTimeout(order_id)
        if response["status"] == "declined":
            raise PaymentDeclined(response.get("reason", "declined"))
        return ChargeResult(response["id"], "succeeded", amount_cents, currency)

    def refund(self, charge_id: str, amount_cents: int) -> Refund:
        response = self._transport.post("/refunds", {"charge": charge_id,
                                                     "amount": amount_cents})
        return Refund(response["id"], charge_id, amount_cents)

    def retrieve(self, charge_id: str) -> ChargeResult:
        response = self._transport.post("/charges/retrieve", {"id": charge_id})
        return ChargeResult(response["id"], response["status"], response["amount"],
                            response["currency"])
''',
    "tests/__init__.py": "",
    "tests/test_client.py": '''
import pytest

from payments import PaymentClient, PaymentDeclined
from payments.transport import FakeTransport


def test_successful_charge():
    client = PaymentClient(FakeTransport([{"status": "succeeded", "id": "ch_1"}]))
    result = client.charge("o1", 500, "EUR", "tok")
    assert result.succeeded and result.charge_id == "ch_1"


def test_declined_charge_raises():
    client = PaymentClient(FakeTransport([{"status": "declined", "reason": "funds"}]))
    with pytest.raises(PaymentDeclined):
        client.charge("o2", 500, "EUR", "tok")
''',
}

FILES_1_5_0: dict[str, str] = dict(FILES_1_4_2)
FILES_1_5_0.update({
    "pyproject.toml": _pyproject("1.5.0"),
    "CHANGELOG.md": FILES_1_4_2["CHANGELOG.md"].replace("# Changelog\n", '''# Changelog

## 1.5.0
- No exceptions for control flow: a declined charge is a normal outcome, so
  `charge()` now returns `ChargeResult(status="declined", decline_reason=...)`
  instead of raising `PaymentDeclined`. Check `result.succeeded`.
  `PaymentDeclined` stays importable for compatibility.
'''),
    "payments/__init__.py": FILES_1_4_2["payments/__init__.py"].replace('"1.4.2"', '"1.5.0"'),
    "payments/models.py": '''
"""Results returned by the client."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChargeResult:
    charge_id: str
    status: str  # "succeeded" | "declined"
    amount_cents: int
    currency: str
    decline_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def declined(self) -> bool:
        return self.status == "declined"


@dataclass(frozen=True)
class Refund:
    refund_id: str
    charge_id: str
    amount_cents: int
''',
    "payments/client.py": FILES_1_4_2["payments/client.py"].replace(
        '''        """Charge a card.

        Raises PaymentDeclined when the provider declines the charge, and
        GatewayTimeout when it does not answer after retries.
        """''',
        '''        """Charge a card.

        A declined charge is returned as `ChargeResult(status="declined")`; check
        `result.succeeded`. Raises GatewayTimeout when the provider does not answer
        after retries.
        """''',
    ).replace(
        '''        if response["status"] == "declined":
            raise PaymentDeclined(response.get("reason", "declined"))
        return ChargeResult(response["id"], "succeeded", amount_cents, currency)''',
        '''        if response["status"] == "declined":
            return ChargeResult(response.get("id", ""), "declined", amount_cents, currency,
                                decline_reason=response.get("reason", "declined"))
        return ChargeResult(response["id"], "succeeded", amount_cents, currency)''',
    ).replace("from payments.errors import GatewayTimeout, PaymentDeclined\n",
              "from payments.errors import GatewayTimeout\n"),
    "tests/test_client.py": '''
from payments import PaymentClient
from payments.transport import FakeTransport


def test_successful_charge():
    client = PaymentClient(FakeTransport([{"status": "succeeded", "id": "ch_1"}]))
    result = client.charge("o1", 500, "EUR", "tok")
    assert result.succeeded and result.charge_id == "ch_1"


def test_declined_charge_is_a_result():
    client = PaymentClient(FakeTransport([{"status": "declined", "reason": "funds"}]))
    result = client.charge("o2", 500, "EUR", "tok")
    assert result.declined and result.decline_reason == "funds"
''',
})
