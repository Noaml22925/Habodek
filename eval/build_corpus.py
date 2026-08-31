"""Generate the synthetic evaluation corpus.

Run once to write `eval/corpus/synthetic/`; the result is ordinary checked-in
files that you can edit by hand afterwards. Regenerating overwrites them.

Expected-finding line numbers are resolved from an `anchor` substring rather than
written by hand, so editing a case's code cannot silently desynchronise its
expectations from the file.

Design notes on the corpus itself:

* Every planted flaw is something a competent human reviewer would raise. Nothing
  here is a trick.
* Roughly a fifth of the cases are **clean** -- a good change with nothing wrong.
  These carry most of the signal, because a reviewer that flags everything scores
  perfect recall and is useless. Without clean cases you cannot see that.
* Several flaws are only visible from *outside* the changed lines -- the caller
  that breaks, the base class whose contract is violated. Those separate a
  reviewer that reads context from one that reads a diff.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

CORPUS = Path(__file__).parent / "corpus" / "synthetic"


def case(
    case_id: str,
    title: str,
    *,
    commit_message: str,
    before: dict[str, str],
    after: dict[str, str],
    expect: list[dict[str, Any]] | None = None,
    tolerate: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    return {
        "id": case_id,
        "title": title,
        "commit_message": commit_message,
        "before": before,
        "after": after,
        "expect": expect or [],
        "tolerate": tolerate or [],
        "notes": notes,
    }


CASES: list[dict[str, Any]] = [
    # ---------------------------------------------------------------- correctness
    case(
        "swallowed-exception",
        "Gateway errors swallowed, turning an outage into a zero balance",
        commit_message="Make charge lookup resilient to gateway errors",
        before={
            "src/payments.py": '''from decimal import Decimal


class PaymentGateway:
    """Reads charge history from the upstream payment provider."""

    def __init__(self, client):
        self._client = client

    def charges_for(self, customer_id: str) -> list[dict]:
        response = self._client.get(f"/customers/{customer_id}/charges")
        return response["data"]

    def total_charged(self, customer_id: str) -> Decimal:
        return sum(Decimal(c["amount"]) for c in self.charges_for(customer_id))
'''
        },
        after={
            "src/payments.py": '''from decimal import Decimal


class PaymentGateway:
    """Reads charge history from the upstream payment provider."""

    def __init__(self, client):
        self._client = client

    def charges_for(self, customer_id: str) -> list[dict]:
        try:
            response = self._client.get(f"/customers/{customer_id}/charges")
            return response["data"]
        except Exception:
            return []

    def total_charged(self, customer_id: str) -> Decimal:
        return sum(Decimal(c["amount"]) for c in self.charges_for(customer_id))
'''
        },
        expect=[
            {
                "id": "swallowed",
                "category": "correctness",
                "severity": "blocker",
                "file": "src/payments.py",
                "anchor": "except Exception:",
                "span": 2,
                "summary": (
                    "Catching bare Exception and returning [] makes a gateway outage "
                    "indistinguishable from a customer with no charges. total_charged "
                    "then reports 0 for a customer who owes money."
                ),
                "keywords": ["swallow", "except", "silent", "empty", "outage", "mask"],
            }
        ],
        notes="The damage is in total_charged, which is unchanged. Requires reading past the diff.",
    ),
    case(
        "mutable-default-argument",
        "Mutable default argument accumulates across calls",
        commit_message="Add default event list to webhook registration",
        before={
            "src/webhooks.py": """def register_webhook(url: str, events: list[str]) -> dict:
    if not url.startswith("https://"):
        raise ValueError("webhook url must be https")
    return {"url": url, "events": events}
"""
        },
        after={
            "src/webhooks.py": """def register_webhook(url: str, events: list[str] = []) -> dict:
    if not url.startswith("https://"):
        raise ValueError("webhook url must be https")
    events.append("ping")
    return {"url": url, "events": events}
"""
        },
        expect=[
            {
                "id": "mutable-default",
                "category": "correctness",
                "severity": "major",
                "file": "src/webhooks.py",
                "anchor": "events: list[str] = []",
                "span": 1,
                "summary": (
                    "The default list is created once at definition time and mutated by "
                    "append, so every caller that omits `events` shares one growing list."
                ),
                "keywords": ["mutable default", "default argument", "shared", "append"],
            }
        ],
    ),
    case(
        "sql-injection",
        "User-controlled value interpolated into SQL",
        commit_message="Add order filtering by status",
        before={
            "src/orders.py": """class OrderRepository:
    def __init__(self, db):
        self._db = db

    def all_orders(self) -> list[dict]:
        return self._db.execute("SELECT * FROM orders").fetchall()
"""
        },
        after={
            "src/orders.py": """class OrderRepository:
    def __init__(self, db):
        self._db = db

    def all_orders(self) -> list[dict]:
        return self._db.execute("SELECT * FROM orders").fetchall()

    def find_by_status(self, status: str) -> list[dict]:
        query = f"SELECT * FROM orders WHERE status = '{status}'"
        return self._db.execute(query).fetchall()
"""
        },
        expect=[
            {
                "id": "sqli",
                "category": "security",
                "severity": "blocker",
                "file": "src/orders.py",
                "anchor": 'query = f"SELECT * FROM orders WHERE status',
                "span": 1,
                "summary": (
                    "`status` is interpolated straight into SQL. Use a parameterised query instead."
                ),
                "keywords": ["injection", "sql", "parameteri", "bind", "escape"],
            }
        ],
    ),
    case(
        "off-by-one-pagination",
        "Pagination window overlaps by one row",
        commit_message="Switch pagination to 1-based page numbers",
        before={
            "src/paging.py": '''def page(items: list, page_number: int, per_page: int) -> list:
    """Return one page of items. `page_number` is zero-based."""
    start = page_number * per_page
    end = start + per_page
    return items[start:end]
'''
        },
        after={
            "src/paging.py": '''def page(items: list, page_number: int, per_page: int) -> list:
    """Return one page of items. `page_number` is one-based."""
    start = (page_number - 1) * per_page
    end = start + per_page + 1
    return items[start:end]
'''
        },
        expect=[
            {
                "id": "off-by-one",
                "category": "correctness",
                "severity": "major",
                "file": "src/paging.py",
                "anchor": "end = start + per_page + 1",
                "span": 1,
                "summary": (
                    "`+ 1` makes each page return per_page + 1 items, so the last row of "
                    "every page repeats as the first row of the next."
                ),
                # "+1" and "one too many" describe the same defect as "off-by-one"
                # and are the phrasings a reviewer reaches for when it names the
                # arithmetic rather than the class of bug.
                "keywords": [
                    "off-by-one",
                    "off by one",
                    "overlap",
                    "duplicate",
                    "extra",
                    "+1",
                    "+ 1",
                    "one too many",
                ],
            }
        ],
    ),
    # ---------------------------------------------------------------- performance
    case(
        "n-plus-one-query",
        "Query issued per loop iteration",
        commit_message="Add order summary endpoint",
        before={
            "src/reports.py": """class SummaryService:
    def __init__(self, db):
        self._db = db

    def order_count(self) -> int:
        return self._db.count_orders()
"""
        },
        after={
            "src/reports.py": """class SummaryService:
    def __init__(self, db):
        self._db = db

    def order_count(self) -> int:
        return self._db.count_orders()

    def order_summaries(self, order_ids: list[int]) -> list[dict]:
        summaries = []
        for order_id in order_ids:
            order = self._db.get_order(order_id)
            customer = self._db.get_customer(order["customer_id"])
            summaries.append({"order": order_id, "customer": customer["name"]})
        return summaries
"""
        },
        expect=[
            {
                "id": "n-plus-one",
                "category": "perf",
                "severity": "major",
                "file": "src/reports.py",
                "anchor": "customer = self._db.get_customer(",
                "span": 2,
                "summary": (
                    "Two queries per order id. Fetch the orders and their customers in "
                    "bulk instead."
                ),
                "keywords": ["n+1", "n + 1", "per iteration", "loop", "bulk", "batch"],
            }
        ],
    ),
    # ---------------------------------------------------------------- design / SOLID
    case(
        "srp-fat-handler",
        "One function parses, validates, persists, and emails",
        commit_message="Add signup handler",
        before={
            "src/signup.py": """class SignupHandler:
    def __init__(self, db, mailer):
        self._db = db
        self._mailer = mailer
"""
        },
        after={
            "src/signup.py": """class SignupHandler:
    def __init__(self, db, mailer):
        self._db = db
        self._mailer = mailer

    def handle(self, raw_body: bytes) -> dict:
        import json

        payload = json.loads(raw_body.decode("utf-8"))
        email = payload.get("email", "").strip().lower()
        if "@" not in email:
            return {"status": 400, "error": "bad email"}
        if len(payload.get("password", "")) < 8:
            return {"status": 400, "error": "weak password"}
        existing = self._db.execute(
            "SELECT id FROM users WHERE email = %s", (email,)
        ).fetchone()
        if existing:
            return {"status": 409, "error": "exists"}
        user_id = self._db.execute(
            "INSERT INTO users (email, password) VALUES (%s, %s) RETURNING id",
            (email, payload["password"]),
        ).fetchone()[0]
        body = f"Welcome {email}, your account is ready."
        self._mailer.send(to=email, subject="Welcome", body=body)
        return {"status": 201, "id": user_id}
"""
        },
        expect=[
            {
                "id": "srp",
                "category": "design",
                "severity": "major",
                "file": "src/signup.py",
                "anchor": "def handle(self, raw_body: bytes) -> dict:",
                "span": 4,
                "summary": (
                    "HTTP parsing, validation, persistence, and email delivery in one "
                    "method. None of it can be tested or reused independently."
                ),
                "keywords": [
                    "single responsibility",
                    "srp",
                    "too many",
                    "separate",
                    "extract",
                    "concern",
                ],
            },
            {
                "id": "plaintext-password",
                "category": "security",
                "severity": "blocker",
                "file": "src/signup.py",
                "anchor": '(email, payload["password"]),',
                "span": 1,
                "summary": "The password is stored as submitted, without hashing.",
                "keywords": ["hash", "plain", "bcrypt", "argon", "password"],
            },
        ],
        tolerate=["import inside function body"],
    ),
    case(
        "dip-hardcoded-dependency",
        "Service constructs its own concrete database connection",
        commit_message="Add report service",
        before={
            "src/reporting.py": """class ReportService:
    def __init__(self, connection):
        self._connection = connection

    def monthly_totals(self) -> list[dict]:
        return self._connection.query("SELECT month, total FROM revenue")
"""
        },
        after={
            "src/reporting.py": """from infra.postgres import PostgresConnection


class ReportService:
    def __init__(self):
        self._connection = PostgresConnection(host="prod-db-01", port=5432)

    def monthly_totals(self) -> list[dict]:
        return self._connection.query("SELECT month, total FROM revenue")
"""
        },
        expect=[
            {
                "id": "dip",
                "category": "design",
                "severity": "major",
                "file": "src/reporting.py",
                "anchor": "self._connection = PostgresConnection(",
                "span": 1,
                "summary": (
                    "Replacing injected collaborator with a concrete instance built "
                    "in-place. The service can no longer be tested without a real "
                    "database, and the production host is hardcoded."
                ),
                "keywords": [
                    "inject",
                    "dependency",
                    "concrete",
                    "hardcod",
                    "coupl",
                    "untestable",
                    "test",
                ],
            }
        ],
        notes="Also a regression: the previous version took the connection as a parameter.",
    ),
    case(
        "lsp-violation",
        "Subclass refuses a method its base class promises",
        commit_message="Add read-only account type",
        before={
            "src/accounts.py": '''class Account:
    def __init__(self, balance: int = 0):
        self.balance = balance

    def deposit(self, amount: int) -> None:
        self.balance += amount

    def withdraw(self, amount: int) -> None:
        if amount > self.balance:
            raise ValueError("insufficient funds")
        self.balance -= amount


def drain(account: Account) -> int:
    """Withdraw everything and return the amount taken."""
    amount = account.balance
    account.withdraw(amount)
    return amount
'''
        },
        after={
            "src/accounts.py": '''class Account:
    def __init__(self, balance: int = 0):
        self.balance = balance

    def deposit(self, amount: int) -> None:
        self.balance += amount

    def withdraw(self, amount: int) -> None:
        if amount > self.balance:
            raise ValueError("insufficient funds")
        self.balance -= amount


class ReadOnlyAccount(Account):
    def deposit(self, amount: int) -> None:
        raise NotImplementedError("read-only account")

    def withdraw(self, amount: int) -> None:
        raise NotImplementedError("read-only account")


def drain(account: Account) -> int:
    """Withdraw everything and return the amount taken."""
    amount = account.balance
    account.withdraw(amount)
    return amount
'''
        },
        expect=[
            {
                "id": "lsp",
                "category": "design",
                "severity": "major",
                "file": "src/accounts.py",
                "anchor": "class ReadOnlyAccount(Account):",
                "span": 6,
                "summary": (
                    "ReadOnlyAccount cannot stand in for Account: drain() takes an "
                    "Account and will raise for this subclass. Liskov violation -- the "
                    "read-only case wants a narrower base type, not a subclass that "
                    "removes behaviour."
                ),
                "keywords": [
                    "liskov",
                    "lsp",
                    "substitut",
                    "subclass",
                    "contract",
                    "notimplemented",
                ],
            }
        ],
        notes="drain() is unchanged. Only a reviewer reading the whole file sees the break.",
    ),
    # ---------------------------------------------------------------- smells / clean code
    case(
        "duplicated-logic",
        "Normalisation block copy-pasted instead of extracted",
        commit_message="Add company name normalisation to the import path",
        before={
            "src/normalise.py": """def normalise_customer(record: dict) -> dict:
    name = record.get("name", "")
    name = name.strip().lower()
    name = name.replace("  ", " ")
    name = name.removesuffix(" ltd").removesuffix(" inc")
    return {**record, "name": name}
"""
        },
        after={
            "src/normalise.py": """def normalise_customer(record: dict) -> dict:
    name = record.get("name", "")
    name = name.strip().lower()
    name = name.replace("  ", " ")
    name = name.removesuffix(" ltd").removesuffix(" inc")
    return {**record, "name": name}


def normalise_supplier(record: dict) -> dict:
    name = record.get("company", "")
    name = name.strip().lower()
    name = name.replace("  ", " ")
    name = name.removesuffix(" ltd").removesuffix(" inc")
    return {**record, "company": name}
"""
        },
        expect=[
            {
                "id": "duplication",
                "category": "smell",
                "severity": "minor",
                "file": "src/normalise.py",
                "anchor": "def normalise_supplier(record: dict) -> dict:",
                "span": 6,
                "summary": (
                    "Four lines of normalisation duplicated verbatim. Extract a helper "
                    "so the two paths cannot drift apart."
                ),
                "keywords": ["duplicat", "dry", "extract", "repeat", "copy"],
            }
        ],
    ),
    case(
        "magic-numbers-nesting",
        "Unexplained thresholds buried in nested conditionals",
        commit_message="Add promotion eligibility check",
        before={
            "src/eligibility.py": """class User:
    def __init__(self, age: int, balance: int, days_active: int):
        self.age = age
        self.balance = balance
        self.days_active = days_active
"""
        },
        after={
            "src/eligibility.py": """class User:
    def __init__(self, age: int, balance: int, days_active: int):
        self.age = age
        self.balance = balance
        self.days_active = days_active


def is_eligible(user: User) -> bool:
    if user.age > 17:
        if user.balance > 500:
            if user.days_active > 30:
                return True
            else:
                return False
        else:
            return False
    else:
        return False
"""
        },
        expect=[
            {
                "id": "magic-numbers",
                "category": "smell",
                "severity": "minor",
                "file": "src/eligibility.py",
                "anchor": "if user.age > 17:",
                "span": 10,
                "summary": (
                    "Three unexplained thresholds nested four deep, with if/else "
                    "returning literal booleans. Name the constants and collapse to a "
                    "single boolean expression."
                ),
                "keywords": [
                    "magic number",
                    "constant",
                    "nest",
                    "boolean",
                    "simplif",
                    "return",
                ],
            }
        ],
    ),
    case(
        "typos",
        "Misspelled identifier and user-facing string",
        commit_message="Add receipt total helper",
        before={
            "src/receipts.py": """def format_receipt(items: list[dict]) -> str:
    lines = [f"{i['name']}: {i['price']}" for i in items]
    return "\\n".join(lines)
"""
        },
        after={
            "src/receipts.py": """def calculate_recieved_total(items: list[dict]) -> int:
    return sum(i["price"] for i in items)


def format_receipt(items: list[dict]) -> str:
    lines = [f"{i['name']}: {i['price']}" for i in items]
    lines.append("Payment recieved successfuly")
    return "\\n".join(lines)
"""
        },
        expect=[
            {
                "id": "typo-identifier",
                "category": "typo",
                "severity": "nit",
                "file": "src/receipts.py",
                "anchor": "def calculate_recieved_total(",
                "span": 1,
                "summary": "`recieved` should be `received`.",
                "keywords": ["recieved", "received", "spell", "typo"],
            },
            {
                "id": "typo-string",
                "category": "typo",
                "severity": "minor",
                "file": "src/receipts.py",
                "anchor": 'lines.append("Payment recieved successfuly")',
                "span": 1,
                "summary": (
                    "User-facing string has two misspellings: `recieved` and `successfuly`."
                ),
                "keywords": ["successfuly", "successfully", "recieved", "spell", "typo"],
            },
        ],
    ),
    # ---------------------------------------------------------------- clean changes
    case(
        "clean-extract-method",
        "CLEAN: a genuine refactor with no defects",
        commit_message="Extract tax calculation from invoice total",
        before={
            "src/invoice.py": """from decimal import Decimal

STANDARD_RATE = Decimal("0.20")


def invoice_total(lines: list[dict]) -> Decimal:
    subtotal = sum(Decimal(str(line["amount"])) for line in lines)
    tax = subtotal * STANDARD_RATE
    return subtotal + tax
"""
        },
        after={
            "src/invoice.py": """from decimal import Decimal

STANDARD_RATE = Decimal("0.20")


def subtotal_of(lines: list[dict]) -> Decimal:
    return sum(Decimal(str(line["amount"])) for line in lines)


def tax_on(subtotal: Decimal) -> Decimal:
    return subtotal * STANDARD_RATE


def invoice_total(lines: list[dict]) -> Decimal:
    subtotal = subtotal_of(lines)
    return subtotal + tax_on(subtotal)
"""
        },
        expect=[],
        notes="Behaviour-preserving extraction. The correct review is silence.",
    ),
    case(
        "clean-guard-clause",
        "CLEAN: adds validation and its test",
        commit_message="Reject negative quantities in cart updates",
        before={
            "src/cart.py": """def set_quantity(cart: dict, sku: str, quantity: int) -> dict:
    cart[sku] = quantity
    return cart
""",
            "tests/test_cart.py": """from src.cart import set_quantity


def test_sets_quantity():
    assert set_quantity({}, "abc", 2) == {"abc": 2}
""",
        },
        after={
            "src/cart.py": """def set_quantity(cart: dict, sku: str, quantity: int) -> dict:
    if quantity < 0:
        raise ValueError("quantity must not be negative")
    cart[sku] = quantity
    return cart
""",
            "tests/test_cart.py": """import pytest

from src.cart import set_quantity


def test_sets_quantity():
    assert set_quantity({}, "abc", 2) == {"abc": 2}


def test_rejects_negative_quantity():
    with pytest.raises(ValueError, match="must not be negative"):
        set_quantity({}, "abc", -1)
""",
        },
        expect=[],
        notes="Correct guard clause, tested. A reviewer that finds fault here is noisy.",
    ),
]


def _resolve_line(content: str, anchor: str, case_id: str, file: str) -> int:
    for index, line in enumerate(content.splitlines(), start=1):
        if anchor in line:
            return index
    raise ValueError(f"{case_id}: anchor {anchor!r} not found in {file}")


def build() -> int:
    if CORPUS.exists():
        shutil.rmtree(CORPUS)
    CORPUS.mkdir(parents=True)

    for spec in CASES:
        target = CORPUS / spec["id"]
        for side in ("before", "after"):
            for rel_path, content in spec[side].items():
                path = target / side / rel_path
                path.parent.mkdir(parents=True, exist_ok=True)
                # newline="\n" so a rebuild on Windows does not rewrite every
                # corpus file with CRLF. Without it `git diff` shows all 41
                # files after any edit, which destroys the property this
                # generator exists for: that changing one case's keywords shows
                # up as exactly one changed case.yaml.
                path.write_text(content, encoding="utf-8", newline="\n")

        expectations = []
        for item in spec["expect"]:
            after_content = spec["after"][item["file"]]
            start = _resolve_line(after_content, item["anchor"], spec["id"], item["file"])
            expectations.append(
                {
                    "id": item["id"],
                    "category": item["category"],
                    "severity": item["severity"],
                    "file": item["file"],
                    "lines": [start, start + item.get("span", 1) - 1],
                    "summary": item["summary"],
                    "keywords": item["keywords"],
                }
            )

        meta = {
            "id": spec["id"],
            "title": spec["title"],
            "commit_message": spec["commit_message"],
            "expect": expectations,
            "tolerate": spec["tolerate"],
            "notes": spec["notes"],
        }
        (target / "case.yaml").write_text(
            yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=88),
            encoding="utf-8",
            newline="\n",
        )

    return len(CASES)


if __name__ == "__main__":
    count = build()
    planted = sum(len(c["expect"]) for c in CASES)
    clean = sum(1 for c in CASES if not c["expect"])
    print(f"wrote {count} cases to {CORPUS}")
    print(f"  {planted} planted flaws, {clean} clean cases")
