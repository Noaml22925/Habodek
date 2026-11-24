"""Source of the `orders-service` consumer, and the variants its scenario branches add.

It pins both libraries. The parts the scenarios are about:

- `orders/api/profile.py`, `orders/jobs/digest.py` call `get_user` and use `.email`.
- `orders/domain/checkout.py` relies on `PaymentDeclined` to cancel an order.
- `orders/api/v2.py` serves refunds behind the `owns_refund` middleware, registered
  on the `/api/v2/refunds` prefix in `orders/app.py`.
- Quantity validation is inline at four call sites; `orders/schema.py` guarantees the
  quantity is an int before any of them runs.

Distractors: pricing, inventory, fulfilment, notifications, settings, import jobs.
"""

from __future__ import annotations

REQUIREMENTS = "platform-common==2.3.0\npayments-lib==1.4.2\n"

FILES: dict[str, str] = {
    "requirements.txt": REQUIREMENTS,
    "pyproject.toml": '''
[project]
name = "orders-service"
version = "0.9.0"
description = "Orders, checkout and refunds."
requires-python = ">=3.10"
''',
    "README.md": '''
# orders-service

HTTP API and jobs for orders, checkout and refunds. Built on `platform-common`
(routing, auth, storage) and `payments-lib` (the payment provider).

- `orders/app.py` — builds the router: blueprints and prefix middleware.
- `orders/api/` — v1 handlers; `v2.py` — the v2 refunds API.
- `orders/domain/` — cart, pricing, checkout, fulfilment.
- `orders/jobs/` — the weekly digest and the CSV order import.
''',
    "orders/__init__.py": "",
    "orders/settings.py": '''
"""Service settings."""

MAX_QUANTITY = 100
CURRENCY = "EUR"
DIGEST_BATCH = 500
''',
    "orders/schema.py": '''
"""Request schemas: every payload is parsed here before a handler sees it.

`OrderLine.quantity` is always an int after parsing: a missing, null or non-integer
quantity is rejected with a ValidationError (HTTP 400) at this layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from platform_common.errors import ValidationError


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int


def _int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    return value


def parse_order_line(data: dict[str, Any]) -> OrderLine:
    sku = data.get("sku")
    if not isinstance(sku, str) or not sku:
        raise ValidationError("sku is required")
    if "quantity" not in data:
        raise ValidationError("quantity is required")
    return OrderLine(sku, _int(data["quantity"], "quantity"))


def parse_import_row(row: dict[str, str]) -> OrderLine:
    """CSV rows carry strings; convert, then validate like any other payload."""
    raw = (row.get("quantity") or "").strip()
    try:
        quantity = int(raw)
    except ValueError:
        raise ValidationError(f"quantity {raw!r} is not an integer") from None
    return parse_order_line({"sku": row.get("sku"), "quantity": quantity})
''',
    "orders/validation.py": '''
"""Field validation shared by handlers and jobs."""

from __future__ import annotations

import re

from platform_common.errors import ValidationError

_SKU = re.compile(r"^[A-Z]{3}-\\d{4}$")


def validate_sku(sku: str) -> None:
    if not _SKU.match(sku):
        raise ValidationError(f"bad sku {sku!r}")
''',
    "orders/subscriptions.py": '''
"""Who receives the weekly digest."""

from __future__ import annotations

_SUBSCRIBERS: set[int] = set()


def subscribe(uid: int) -> None:
    _SUBSCRIBERS.add(uid)


def remove(uid: int) -> None:
    _SUBSCRIBERS.discard(uid)


def subscribers() -> list[int]:
    return sorted(_SUBSCRIBERS)
''',
    "orders/notify.py": '''
"""Outgoing email (queued; the mailer service sends it)."""

from __future__ import annotations

import logging

log = logging.getLogger("orders.notify")
OUTBOX: list[tuple[str, str]] = []


def send(email: str, subject: str) -> None:
    OUTBOX.append((email, subject))
    log.info("queued %r to %s", subject, email)


def send_digest(email: str, orders: list) -> None:
    send(email, f"Your week: {len(orders)} orders")


def order_cancelled(order) -> None:
    log.info("order %s cancelled: %s", order.id, order.cancel_reason)
''',
    "orders/domain/__init__.py": "",
    "orders/domain/models.py": '''
"""Domain objects."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from orders.schema import OrderLine


@dataclass
class Order:
    id: str
    user_id: int
    lines: list[OrderLine] = field(default_factory=list)
    status: str = "pending"  # pending | paid | cancelled | shipped
    total_cents: int = 0
    charge_id: str | None = None
    cancel_reason: str | None = None


@dataclass
class Refund:
    id: str
    order_id: str
    user_id: int
    amount_cents: int
    status: str = "requested"

    def as_dict(self) -> dict:
        return asdict(self)
''',
    "orders/domain/pricing.py": '''
"""Prices, in cents."""

from __future__ import annotations

from orders.schema import OrderLine

PRICES = {"BOK-0001": 1299, "BOK-0002": 2499, "MUG-0001": 899, "TEE-0001": 1999}


def price_cents(sku: str) -> int:
    return PRICES.get(sku, 0)


def total(lines: list[OrderLine]) -> int:
    return sum(price_cents(line.sku) * line.quantity for line in lines)
''',
    "orders/domain/inventory.py": '''
"""Stock levels."""

from __future__ import annotations

STOCK: dict[str, int] = {"BOK-0001": 40, "BOK-0002": 12, "MUG-0001": 200, "TEE-0001": 75}


def available(sku: str) -> int:
    return STOCK.get(sku, 0)


def reserve(sku: str, quantity: int) -> bool:
    if STOCK.get(sku, 0) < quantity:
        return False
    STOCK[sku] -= quantity
    return True
''',
    "orders/domain/cart.py": '''
"""The shopping cart."""

from __future__ import annotations

from platform_common.errors import ValidationError

from orders.domain import inventory
from orders.schema import OrderLine
from orders.settings import MAX_QUANTITY


class Cart:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.lines: dict[str, OrderLine] = {}

    def add_item(self, line: OrderLine) -> None:
        if line.quantity <= 0 or line.quantity > MAX_QUANTITY:
            raise ValidationError("quantity out of range")
        if inventory.available(line.sku) < line.quantity:
            raise ValidationError(f"only {inventory.available(line.sku)} left")
        self.lines[line.sku] = line

    def remove_item(self, sku: str) -> None:
        self.lines.pop(sku, None)
''',
    "orders/domain/fulfilment.py": '''
"""Hand paid orders to the warehouse."""

from __future__ import annotations

QUEUE: list[str] = []


def enqueue(order) -> None:
    if order.status != "paid":
        raise ValueError(f"order {order.id} is not paid")
    QUEUE.append(order.id)
''',
    "orders/domain/checkout.py": '''
"""Checkout: charge the card, then mark the order paid or cancelled."""

from __future__ import annotations

from payments import PaymentClient, PaymentDeclined
from platform_common.errors import Conflict

from orders import notify
from orders.domain import fulfilment
from orders.domain.models import Order
from orders.settings import CURRENCY
from orders.storage import orders_store


def checkout(order: Order, card_token: str, client: PaymentClient) -> Order:
    if order.status != "pending":
        raise Conflict(f"order {order.id} is {order.status}")
    try:
        result = client.charge(order.id, order.total_cents, CURRENCY, card_token)
    except PaymentDeclined as exc:
        order.status = "cancelled"
        order.cancel_reason = exc.reason
        orders_store.save(order)
        notify.order_cancelled(order)
        return order
    order.status = "paid"
    order.charge_id = result.charge_id
    orders_store.save(order)
    fulfilment.enqueue(order)
    return order
''',
    "orders/storage/__init__.py": "",
    "orders/storage/orders_store.py": '''
"""Order storage."""

from __future__ import annotations

from platform_common.store.db import Table

from orders.domain.models import Order

_TABLE = Table("orders")


def get(order_id: str) -> Order | None:
    return _TABLE.get(order_id)


def save(order: Order) -> None:
    _TABLE.put(order.id, order)


def list_for_user(uid: int) -> list[Order]:
    return [o for o in _TABLE.scan() if o.user_id == uid]
''',
    "orders/storage/refunds_store.py": '''
"""Refund storage."""

from __future__ import annotations

from platform_common.store.db import Table

from orders.domain.models import Refund

_TABLE = Table("refunds")


def get(refund_id: str) -> Refund | None:
    return _TABLE.get(refund_id)


def save(refund: Refund) -> None:
    _TABLE.put(refund.id, refund)


def list_all() -> list[Refund]:
    return _TABLE.scan()
''',
    "orders/middleware/__init__.py": "",
    "orders/middleware/ownership.py": '''
"""Ownership checks: the caller must own the resource named in the path."""

from __future__ import annotations

from collections.abc import Callable

from platform_common.errors import Forbidden, NotFound, Unauthorized
from platform_common.web.router import Request

from orders.storage import orders_store, refunds_store


def owns_resource(load: Callable[[Request], object | None]) -> Callable[[Request], None]:
    def middleware(request: Request) -> None:
        if request.user_id is None:
            raise Unauthorized("login required")
        resource = load(request)
        if resource is None:
            raise NotFound("no such resource")
        if resource.user_id != request.user_id:
            raise Forbidden("not yours")
        request.resource = resource

    return middleware


owns_order = owns_resource(lambda r: orders_store.get(r.params["order_id"]))
owns_refund = owns_resource(lambda r: refunds_store.get(r.params["refund_id"]))
''',
    "orders/api/__init__.py": "",
    "orders/api/profile.py": '''
"""The caller's profile."""

from __future__ import annotations

from platform_common.errors import Unauthorized
from platform_common.store.users import get_user
from platform_common.web.router import Blueprint, Request, Response

bp = Blueprint("/api/v1")


@bp.route("GET", "/profile")
def profile(request: Request) -> Response:
    if request.user_id is None:
        raise Unauthorized("login required")
    user = get_user(request.user_id)
    return Response(200, {"email": user.email, "name": user.name})
''',
    "orders/api/orders.py": '''
"""Orders API, v1."""

from __future__ import annotations

import uuid

from platform_common.errors import Forbidden, NotFound, Unauthorized, ValidationError
from platform_common.web.router import Blueprint, Request, Response

from orders.domain import pricing
from orders.domain.models import Order
from orders.schema import parse_order_line
from orders.settings import MAX_QUANTITY
from orders.storage import orders_store
from orders.validation import validate_sku

bp = Blueprint("/api/v1")


def _own_order(request: Request) -> Order:
    if request.user_id is None:
        raise Unauthorized("login required")
    order = orders_store.get(request.params["order_id"])
    if order is None:
        raise NotFound("no such order")
    if order.user_id != request.user_id:
        raise Forbidden("not yours")
    return order


@bp.route("POST", "/orders")
def create_order(request: Request) -> Response:
    if request.user_id is None:
        raise Unauthorized("login required")
    line = parse_order_line(request.body)
    validate_sku(line.sku)
    if line.quantity <= 0 or line.quantity > MAX_QUANTITY:
        raise ValidationError("quantity out of range")
    order = Order(uuid.uuid4().hex, request.user_id, [line])
    order.total_cents = pricing.total(order.lines)
    orders_store.save(order)
    return Response(201, {"id": order.id, "total_cents": order.total_cents})


@bp.route("GET", "/orders/{order_id}")
def get_order(request: Request) -> Response:
    order = _own_order(request)
    return Response(200, {"id": order.id, "status": order.status})


@bp.route("PATCH", "/orders/{order_id}/lines")
def update_line(request: Request) -> Response:
    order = _own_order(request)
    line = parse_order_line(request.body)
    if line.quantity <= 0 or line.quantity > MAX_QUANTITY:
        raise ValidationError("quantity out of range")
    order.lines = [l for l in order.lines if l.sku != line.sku] + [line]
    order.total_cents = pricing.total(order.lines)
    orders_store.save(order)
    return Response(200, {"id": order.id, "total_cents": order.total_cents})
''',
    "orders/api/admin.py": '''
"""Admin endpoints. Everything here is under /admin (see AdminBlueprint)."""

from __future__ import annotations

from platform_common.store.users import list_users
from platform_common.web.admin import AdminBlueprint
from platform_common.web.router import Request, Response

admin = AdminBlueprint()


@admin.route("GET", "/users")
def users(request: Request) -> Response:
    return Response(200, [{"id": u.id, "email": u.email} for u in list_users()])
''',
    "orders/api/v2.py": '''
"""Refunds API, v2. Ownership is enforced by middleware registered in orders/app.py."""

from __future__ import annotations

from platform_common.web.router import Blueprint, Request, Response

bp = Blueprint("/api/v2")


@bp.route("GET", "/refunds/{refund_id}")
def get_refund(request: Request) -> Response:
    return Response(200, request.resource.as_dict())
''',
    "orders/app.py": '''
"""Build the service's router."""

from __future__ import annotations

from platform_common.web.middleware import access_log, request_id
from platform_common.web.router import Router

from orders.api import admin, orders, profile, v2
from orders.middleware.ownership import owns_refund


def build_router() -> Router:
    router = Router()
    router.use_prefix("/api", request_id)
    router.use_prefix("/api", access_log)
    router.use_prefix("/api/v2/refunds", owns_refund)
    router.register(profile.bp)
    router.register(orders.bp)
    router.register(v2.bp)
    router.register(admin.admin)
    return router
''',
    "orders/jobs/__init__.py": "",
    "orders/jobs/digest.py": '''
"""The weekly digest email."""

from __future__ import annotations

from platform_common.errors import NotFound
from platform_common.jobs.scheduler import job
from platform_common.store.users import get_user

from orders import notify, subscriptions
from orders.storage import orders_store


@job("weekly-digest", "0 6 * * MON")
def send_weekly_digest() -> int:
    sent = 0
    for uid in subscriptions.subscribers():
        try:
            user = get_user(uid)
        except NotFound:
            subscriptions.remove(uid)  # deleted accounts stop getting mail
            continue
        notify.send_digest(user.email, orders_store.list_for_user(uid))
        sent += 1
    return sent
''',
    "orders/jobs/import_orders.py": '''
"""Import orders from a partner's CSV export."""

from __future__ import annotations

import csv
import io
import uuid

from platform_common.errors import ValidationError

from orders.domain import pricing
from orders.domain.models import Order
from orders.schema import parse_import_row
from orders.settings import MAX_QUANTITY
from orders.storage import orders_store


def import_row(row: dict[str, str], user_id: int) -> Order:
    line = parse_import_row(row)
    if line.quantity <= 0 or line.quantity > MAX_QUANTITY:
        raise ValidationError("quantity out of range")
    order = Order(uuid.uuid4().hex, user_id, [line])
    order.total_cents = pricing.total(order.lines)
    orders_store.save(order)
    return order


def import_csv(text: str, user_id: int) -> tuple[int, list[str]]:
    imported, errors = 0, []
    for number, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        try:
            import_row(row, user_id)
            imported += 1
        except ValidationError as exc:
            errors.append(f"line {number}: {exc}")
    return imported, errors
''',
    "tests/__init__.py": "",
    "tests/test_checkout.py": '''
from payments import PaymentClient
from payments.transport import FakeTransport

from orders.domain.checkout import checkout
from orders.domain.models import Order


def test_paid():
    order = Order("o1", 1, total_cents=500)
    client = PaymentClient(FakeTransport([{"status": "succeeded", "id": "ch_1"}]))
    assert checkout(order, "tok", client).status == "paid"


def test_declined_cancels():
    order = Order("o2", 1, total_cents=500)
    client = PaymentClient(FakeTransport([{"status": "declined", "reason": "funds"}]))
    assert checkout(order, "tok", client).status == "cancelled"
''',
    "tests/test_orders_api.py": '''
from orders.app import build_router
from platform_common.web.router import Request


def test_create_order_rejects_bad_quantity():
    router = build_router()
    response = router.dispatch(Request("POST", "/api/v1/orders", user_id=1,
                                       body={"sku": "BOK-0001", "quantity": 0}))
    assert response.status == 400
''',
}

# --------------------------------------------------------------------------- history

# The first import predates the double-charge fix (no status guard in checkout).
INITIAL_CHECKOUT = FILES["orders/domain/checkout.py"].replace(
    '''    if order.status != "pending":
        raise Conflict(f"order {order.id} is {order.status}")
''', "").replace("from platform_common.errors import Conflict\n", "")

# Before the v2 refunds commit: no v2 API, no ownership middleware, no refunds store.
INITIAL_APP = '''
"""Build the service's router."""

from __future__ import annotations

from platform_common.web.middleware import access_log, request_id
from platform_common.web.router import Router

from orders.api import admin, orders, profile


def build_router() -> Router:
    router = Router()
    router.use_prefix("/api", request_id)
    router.use_prefix("/api", access_log)
    router.register(profile.bp)
    router.register(orders.bp)
    router.register(admin.admin)
    return router
'''

# Before "digest skips deleted subscribers": a deleted subscriber killed the job.
INITIAL_DIGEST = '''
"""The weekly digest email."""

from __future__ import annotations

from platform_common.jobs.scheduler import job
from platform_common.store.users import get_user

from orders import notify, subscriptions
from orders.storage import orders_store


@job("weekly-digest", "0 6 * * MON")
def send_weekly_digest() -> int:
    sent = 0
    for uid in subscriptions.subscribers():
        user = get_user(uid)
        notify.send_digest(user.email, orders_store.list_for_user(uid))
        sent += 1
    return sent
'''

# --------------------------------------------------------------------------- scenario 2
S2_REQUIREMENTS = "platform-common==2.3.0\npayments-lib==1.5.0\n"

# --------------------------------------------------------------------------- scenario 3
S3_VALIDATION = FILES["orders/validation.py"].replace(
    "from platform_common.errors import ValidationError\n",
    "from platform_common.errors import ValidationError\n\nfrom orders.settings import "
    "MAX_QUANTITY\n",
) + '''

def validate_quantity(quantity: int) -> None:
    """Quantities are parsed as ints by orders.schema; check the range."""
    if quantity <= 0 or quantity > MAX_QUANTITY:
        raise ValidationError(f"quantity must be between 1 and {MAX_QUANTITY}")
'''

_INLINE = '''    if line.quantity <= 0 or line.quantity > MAX_QUANTITY:
        raise ValidationError("quantity out of range")
'''
_CALL = "    validate_quantity(line.quantity)\n"

S3_ORDERS = FILES["orders/api/orders.py"].replace(_INLINE, _CALL).replace(
    "from platform_common.errors import Forbidden, NotFound, Unauthorized, ValidationError",
    "from platform_common.errors import Forbidden, NotFound, Unauthorized",
).replace("from orders.settings import MAX_QUANTITY\n", "").replace(
    "from orders.validation import validate_sku",
    "from orders.validation import validate_quantity, validate_sku")

S3_IMPORT = FILES["orders/jobs/import_orders.py"].replace(_INLINE, _CALL).replace(
    "from orders.settings import MAX_QUANTITY\nfrom orders.storage import orders_store",
    "from orders.storage import orders_store\nfrom orders.validation import validate_quantity")

S3_CART = FILES["orders/domain/cart.py"].replace(
    '''        if line.quantity <= 0 or line.quantity > MAX_QUANTITY:
            raise ValidationError("quantity out of range")
''', "        validate_quantity(line.quantity)\n").replace(
    "from orders.settings import MAX_QUANTITY\n",
    "from orders.validation import validate_quantity\n")

# --------------------------------------------------------------------------- scenario 4
S4_ADMIN = FILES["orders/api/admin.py"].replace(
    "from platform_common.web.router import Request, Response\n",
    "from platform_common.web.router import Request, Response\n\n"
    "from orders.storage import refunds_store\n",
) + '''

@admin.route("GET", "/refunds")
def all_refunds(request: Request) -> Response:
    refunds = sorted(refunds_store.list_all(), key=lambda r: r.id)
    return Response(200, [r.as_dict() for r in refunds])
'''

# --------------------------------------------------------------------------- refunds v1
REFUNDS_V1 = '''
"""Refunds API, v1."""

from __future__ import annotations

import uuid

from platform_common.errors import Forbidden, NotFound, Unauthorized
from platform_common.web.router import Blueprint, Request, Response

from orders.domain.models import Refund
from orders.storage import orders_store, refunds_store

bp = Blueprint("/api/v1")


@bp.route("GET", "/refunds/{refund_id}")
def get_refund(request: Request) -> Response:
    refund = refunds_store.get(request.params["refund_id"])
    if refund is None:
        raise NotFound("no such refund")
    return Response(200, refund.as_dict())


@bp.route("POST", "/orders/{order_id}/refunds")
def request_refund(request: Request) -> Response:
    if request.user_id is None:
        raise Unauthorized("login required")
    order = orders_store.get(request.params["order_id"])
    if order is None:
        raise NotFound("no such order")
    if order.user_id != request.user_id:
        raise Forbidden("not yours")
    refund = Refund(uuid.uuid4().hex, order.id, request.user_id, order.total_cents)
    refunds_store.save(refund)
    return Response(201, refund.as_dict())
'''

_APP_IMPORT = "from orders.api import admin, orders, profile, v2\n"
_APP_REGISTER = "    router.register(v2.bp)\n"

# Scenario 9's head: the v1 refunds blueprint, registered, with no ownership middleware.
APP_REFUNDS_V1_UNCOVERED = FILES["orders/app.py"].replace(
    _APP_IMPORT, "from orders.api import admin, orders, profile, refunds, v2\n").replace(
    _APP_REGISTER, _APP_REGISTER + "    router.register(refunds.bp)\n")

# Scenario 8's head: the same, with owns_refund also registered on the v1 prefix.
APP_REFUNDS_V1_COVERED = APP_REFUNDS_V1_UNCOVERED.replace(
    '    router.use_prefix("/api/v2/refunds", owns_refund)\n',
    '    router.use_prefix("/api/v2/refunds", owns_refund)\n'
    '    router.use_prefix("/api/v1/refunds", owns_refund)\n')

# --------------------------------------------------------------------------- scenario 6
# A None check in api/profile.py only (the consumer side of a partial fix).
S6_PROFILE = FILES["orders/api/profile.py"].replace(
    "from platform_common.errors import Unauthorized",
    "from platform_common.errors import NotFound, Unauthorized",
).replace(
    '''    user = get_user(request.user_id)
    return''',
    '''    user = get_user(request.user_id)
    if user is None:
        raise NotFound("no such user")
    return''')
