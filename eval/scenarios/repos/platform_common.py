"""Source of the `platform-common` library (version 2.3.0) used by the scenarios.

A small but real-looking shared library: storage, a cache, a tiny web router with
blueprints and middleware, admin routing, jobs, utilities and tests.
"""

from __future__ import annotations

PYPROJECT = '''
[project]
name = "platform-common"
version = "2.3.0"
description = "Shared storage, caching and web plumbing for our services."
requires-python = ">=3.10"
dependencies = []
'''

FILES: dict[str, str] = {
    "pyproject.toml": PYPROJECT,
    "README.md": '''
# platform-common

Shared building blocks for our Python services:

- `store/` — table-backed storage for users and organisations.
- `cache/` — an LRU cache and a user cache with negative caching.
- `web/` — a tiny router: blueprints, prefix middleware, auth and admin routing.
- `jobs/` — a cron-style job registry.

Contract notes:

- `store.users.get_user(uid)` raises `errors.NotFound` for a missing or deleted user.
  Callers rely on this; the router turns `NotFound` into a 404.
''',
    "CHANGELOG.md": '''
# Changelog

## 2.3.0
- Organisation store (`store.orgs`).
- `AdminBlueprint`: every route under `/admin` requires an admin user.

## 2.2.1
- Fix negative cache poisoning on NotFound in `CachedUsers`.

## 2.2.0
- Router blueprints with prefix middleware.
''',
    "platform_common/__init__.py": '''
"""platform-common: shared storage, caching and web plumbing."""

__version__ = "2.3.0"
''',
    "platform_common/errors.py": '''
"""Errors shared by every service. The router maps them to HTTP statuses."""


class PlatformError(Exception):
    """Base class for errors raised by platform-common."""

    status = 500


class NotFound(PlatformError):
    """The requested object does not exist (HTTP 404)."""

    status = 404


class Forbidden(PlatformError):
    """The caller may not access the object (HTTP 403)."""

    status = 403


class Unauthorized(PlatformError):
    """No authenticated user (HTTP 401)."""

    status = 401


class Conflict(PlatformError):
    """The object exists in a conflicting state (HTTP 409)."""

    status = 409


class ValidationError(PlatformError):
    """The request is malformed (HTTP 400)."""

    status = 400
''',
    "platform_common/store/__init__.py": '''
"""Table-backed storage."""
''',
    "platform_common/store/db.py": '''
"""An in-process table store. Production swaps in the SQL backend with the same API."""

from __future__ import annotations

import threading
from typing import Any


class Table:
    def __init__(self, name: str) -> None:
        self.name = name
        self._rows: dict[Any, Any] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any | None:
        with self._lock:
            return self._rows.get(key)

    def put(self, key: Any, row: Any) -> None:
        with self._lock:
            self._rows[key] = row

    def delete(self, key: Any) -> None:
        with self._lock:
            self._rows.pop(key, None)

    def scan(self) -> list[Any]:
        with self._lock:
            return list(self._rows.values())

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()
''',
    "platform_common/store/users.py": '''
"""User records."""

from __future__ import annotations

from dataclasses import dataclass

from platform_common.errors import Conflict, NotFound
from platform_common.store.db import Table


@dataclass
class User:
    id: int
    email: str
    name: str
    is_admin: bool = False
    deleted: bool = False


_TABLE = Table("users")


def get_user(uid: int) -> User:
    """Return the user with id `uid`.

    Raises NotFound when the user does not exist or was deleted.
    """
    row = _TABLE.get(uid)
    if row is None or row.deleted:
        raise NotFound(f"user {uid}")
    return row


def create_user(uid: int, email: str, name: str, is_admin: bool = False) -> User:
    if _TABLE.get(uid) is not None:
        raise Conflict(f"user {uid} exists")
    user = User(uid, email, name, is_admin)
    _TABLE.put(uid, user)
    return user


def delete_user(uid: int) -> None:
    """Soft-delete: the row stays for audit, reads treat it as missing."""
    user = get_user(uid)
    user.deleted = True
    _TABLE.put(uid, user)


def list_users(include_deleted: bool = False) -> list[User]:
    return [u for u in _TABLE.scan() if include_deleted or not u.deleted]


def reset() -> None:
    """Test helper."""
    _TABLE.clear()
''',
    "platform_common/store/orgs.py": '''
"""Organisation records."""

from __future__ import annotations

from dataclasses import dataclass, field

from platform_common.errors import NotFound
from platform_common.store.db import Table


@dataclass
class Org:
    id: int
    name: str
    member_ids: list[int] = field(default_factory=list)


_TABLE = Table("orgs")


def get_org(org_id: int) -> Org:
    row = _TABLE.get(org_id)
    if row is None:
        raise NotFound(f"org {org_id}")
    return row


def add_member(org_id: int, uid: int) -> None:
    org = get_org(org_id)
    if uid not in org.member_ids:
        org.member_ids.append(uid)
''',
    "platform_common/cache/__init__.py": '''
"""Caches."""
''',
    "platform_common/cache/lru.py": '''
"""A small LRU cache."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


class LRU:
    def __init__(self, capacity: int = 1024) -> None:
        self.capacity = capacity
        self._data: OrderedDict[Any, Any] = OrderedDict()

    def get(self, key: Any, default: Any = None) -> Any:
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: Any, value: Any) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def evict(self, key: Any) -> None:
        self._data.pop(key, None)

    def __contains__(self, key: Any) -> bool:
        return key in self._data
''',
    "platform_common/cache/users.py": '''
"""A read-through user cache with negative caching.

Lookups of missing users are expensive (they hit the primary). A miss is remembered
in `_negative` so a burst of requests for a deleted account costs one lookup.
"""

from __future__ import annotations

from platform_common.cache.lru import LRU
from platform_common.errors import NotFound
from platform_common.store.users import User, get_user


class CachedUsers:
    def __init__(self, capacity: int = 4096) -> None:
        self._lru = LRU(capacity)
        self._negative: set[int] = set()

    def get(self, uid: int) -> User:
        if uid in self._negative:
            raise NotFound(f"user {uid}")
        cached = self._lru.get(uid)
        if cached is not None:
            return cached
        try:
            user = get_user(uid)
        except NotFound:
            self._negative.add(uid)
            raise
        self._lru.put(uid, user)
        return user

    def invalidate(self, uid: int) -> None:
        self._lru.evict(uid)
        self._negative.discard(uid)
''',
    "platform_common/web/__init__.py": '''
"""A tiny router with blueprints and middleware."""
''',
    "platform_common/web/router.py": '''
"""Routing: requests, responses, blueprints with prefix middleware.

A middleware is `fn(request) -> None`; it may set attributes on the request or raise
a PlatformError, which ends the request with that error's status.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from platform_common.errors import NotFound, PlatformError

Middleware = Callable[["Request"], None]
Handler = Callable[["Request"], "Response"]


@dataclass
class Request:
    method: str
    path: str
    user_id: int | None = None
    body: dict[str, Any] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    user: Any = None


@dataclass
class Response:
    status: int
    body: Any = None


@dataclass
class Route:
    method: str
    pattern: str
    handler: Handler
    middleware: list[Middleware]
    regex: re.Pattern[str] = field(init=False)

    def __post_init__(self) -> None:
        self.regex = re.compile("^" + re.sub(r"{(\\w+)}", r"(?P<\\1>[^/]+)", self.pattern) + "$")


class Blueprint:
    """Routes under a common prefix, sharing middleware registered with `use`."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix.rstrip("/")
        self.middleware: list[Middleware] = []
        self.routes: list[tuple[str, str, Handler, list[Middleware]]] = []

    def use(self, middleware: Middleware) -> None:
        self.middleware.append(middleware)

    def route(self, method: str, path: str, *, middleware: list[Middleware] | None = None):
        def decorator(handler: Handler) -> Handler:
            self.routes.append((method, path, handler, list(middleware or [])))
            return handler

        return decorator


class Router:
    def __init__(self) -> None:
        self.routes: list[Route] = []
        self.prefix_middleware: list[tuple[str, Middleware]] = []

    def use_prefix(self, prefix: str, middleware: Middleware) -> None:
        """Run `middleware` for every route whose path starts with `prefix`."""
        self.prefix_middleware.append((prefix.rstrip("/"), middleware))

    def register(self, blueprint: Blueprint) -> None:
        for method, path, handler, own in blueprint.routes:
            pattern = blueprint.prefix + path
            # Blueprint middleware is captured at registration time.
            chain = [*blueprint.middleware, *own]
            self.routes.append(Route(method, pattern, handler, chain))

    def _chain(self, route: Route) -> list[Middleware]:
        by_prefix = [m for prefix, m in self.prefix_middleware
                     if route.pattern == prefix or route.pattern.startswith(prefix + "/")]
        return [*by_prefix, *route.middleware]

    def dispatch(self, request: Request) -> Response:
        for route in self.routes:
            if route.method != request.method:
                continue
            match = route.regex.match(request.path)
            if not match:
                continue
            request.params = match.groupdict()
            try:
                for middleware in self._chain(route):
                    middleware(request)
                return route.handler(request)
            except PlatformError as exc:
                return Response(exc.status, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - last-resort 500
                return Response(500, {"error": type(exc).__name__})
        return Response(404, {"error": f"no route for {request.method} {request.path}"})


def not_found(_: Request) -> Response:
    raise NotFound("not found")
''',
    "platform_common/web/auth.py": '''
"""Authentication and authorisation middleware."""

from __future__ import annotations

from platform_common.errors import Forbidden, Unauthorized
from platform_common.store.users import get_user
from platform_common.web.router import Request


def require_user(request: Request) -> None:
    """Load the authenticated user onto `request.user`.

    A token for a deleted or unknown user is a NotFound, which the router serves as 404.
    """
    if request.user_id is None:
        raise Unauthorized("login required")
    request.user = get_user(request.user_id)


def require_admin(request: Request) -> None:
    """Allow only admins. Must run after `require_user`."""
    if request.user is None:
        require_user(request)
    if not request.user.is_admin:
        raise Forbidden("admin only")
''',
    "platform_common/web/admin.py": '''
"""Admin routing: everything under /admin needs an authenticated admin."""

from __future__ import annotations

from platform_common.web.auth import require_admin, require_user
from platform_common.web.router import Blueprint


class AdminBlueprint(Blueprint):
    """A blueprint mounted at `/admin` (or below it).

    `require_user` and `require_admin` are registered on the blueprint itself, so
    every route added with `.route(...)` gets both, whatever the handler does.
    """

    def __init__(self, prefix: str = "/admin") -> None:
        if not prefix.startswith("/admin"):
            raise ValueError("admin blueprints live under /admin")
        super().__init__(prefix)
        self.use(require_user)
        self.use(require_admin)
''',
    "platform_common/web/middleware.py": '''
"""General-purpose middleware."""

from __future__ import annotations

import logging
import uuid

from platform_common.web.router import Request

log = logging.getLogger("platform_common.web")


def request_id(request: Request) -> None:
    request.body.setdefault("_request_id", uuid.uuid4().hex[:12])


def access_log(request: Request) -> None:
    log.info("%s %s user=%s", request.method, request.path, request.user_id)
''',
    "platform_common/jobs/__init__.py": '''
"""Cron-style jobs."""
''',
    "platform_common/jobs/scheduler.py": '''
"""A registry of periodic jobs, run by the service's worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Job:
    name: str
    schedule: str  # cron expression
    fn: Callable[[], None]


_JOBS: dict[str, Job] = {}


def job(name: str, schedule: str):
    def decorator(fn: Callable[[], None]) -> Callable[[], None]:
        _JOBS[name] = Job(name, schedule, fn)
        return fn

    return decorator


def registered() -> list[Job]:
    return sorted(_JOBS.values(), key=lambda j: j.name)


def run(name: str) -> None:
    _JOBS[name].fn()
''',
    "platform_common/util/__init__.py": "",
    "platform_common/util/strings.py": '''
"""String helpers."""

from __future__ import annotations

import re

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")


def truncate(text: str, limit: int, suffix: str = "...") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return (name[:1] + "***@" + domain) if domain else "***"
''',
    "platform_common/util/timeutil.py": '''
"""Time helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="seconds")
''',
    "tests/__init__.py": "",
    "tests/test_users.py": '''
import pytest

from platform_common.errors import NotFound
from platform_common.store import users


def setup_function():
    users.reset()


def test_get_user_returns_the_user():
    users.create_user(1, "a@example.com", "A")
    assert users.get_user(1).email == "a@example.com"


def test_missing_user_raises_not_found():
    with pytest.raises(NotFound):
        users.get_user(404)


def test_deleted_user_raises_not_found():
    users.create_user(2, "b@example.com", "B")
    users.delete_user(2)
    with pytest.raises(NotFound):
        users.get_user(2)
''',
    "tests/test_cache.py": '''
import pytest

from platform_common.cache.users import CachedUsers
from platform_common.errors import NotFound
from platform_common.store import users


def setup_function():
    users.reset()


def test_miss_is_negatively_cached():
    cache = CachedUsers()
    with pytest.raises(NotFound):
        cache.get(7)
    assert 7 in cache._negative


def test_hit_is_cached():
    users.create_user(3, "c@example.com", "C")
    cache = CachedUsers()
    assert cache.get(3).name == "C"
    users.delete_user(3)
    assert cache.get(3).name == "C"  # until invalidated
''',
    "tests/test_router.py": '''
from platform_common.store import users
from platform_common.web.admin import AdminBlueprint
from platform_common.web.router import Request, Router


def setup_function():
    users.reset()


def test_admin_routes_require_admin():
    users.create_user(1, "a@example.com", "A", is_admin=False)
    admin = AdminBlueprint()

    @admin.route("GET", "/stats")
    def stats(request):
        from platform_common.web.router import Response
        return Response(200, {"ok": True})

    router = Router()
    router.register(admin)
    assert router.dispatch(Request("GET", "/admin/stats", user_id=1)).status == 403
    assert router.dispatch(Request("GET", "/admin/stats")).status == 401
''',
}


# Scenario 1: get_user returns None instead of raising.
S1_USERS = FILES["platform_common/store/users.py"].replace(
    '''def get_user(uid: int) -> User:
    """Return the user with id `uid`.

    Raises NotFound when the user does not exist or was deleted.
    """
    row = _TABLE.get(uid)
    if row is None or row.deleted:
        raise NotFound(f"user {uid}")
    return row''',
    '''def get_user(uid: int) -> User | None:
    """Return the user with id `uid`, or None when the user does not exist or was
    deleted (like `dict.get`)."""
    row = _TABLE.get(uid)
    if row is None or row.deleted:
        return None
    return row''',
).replace(
    '''def delete_user(uid: int) -> None:
    """Soft-delete: the row stays for audit, reads treat it as missing."""
    user = get_user(uid)
    user.deleted = True''',
    '''def delete_user(uid: int) -> None:
    """Soft-delete: the row stays for audit, reads treat it as missing."""
    user = get_user(uid)
    if user is None:
        raise NotFound(f"user {uid}")
    user.deleted = True''',
)

S1_TESTS = FILES["tests/test_users.py"].replace(
    '''def test_missing_user_raises_not_found():
    with pytest.raises(NotFound):
        users.get_user(404)


def test_deleted_user_raises_not_found():
    users.create_user(2, "b@example.com", "B")
    users.delete_user(2)
    with pytest.raises(NotFound):
        users.get_user(2)''',
    '''def test_missing_user_returns_none():
    assert users.get_user(404) is None


def test_deleted_user_returns_none():
    users.create_user(2, "b@example.com", "B")
    users.delete_user(2)
    assert users.get_user(2) is None


def test_deleting_a_missing_user_raises():
    with pytest.raises(NotFound):
        users.delete_user(404)''',
)

# The MR also "updates" the cache test: a miss now returns None, and the assertion
# about negative caching goes away with the pytest.raises it sat under.
S1_CACHE_TESTS = FILES["tests/test_cache.py"].replace(
    '''def test_miss_is_negatively_cached():
    cache = CachedUsers()
    with pytest.raises(NotFound):
        cache.get(7)
    assert 7 in cache._negative''',
    '''def test_miss_returns_none():
    cache = CachedUsers()
    assert cache.get(7) is None''',
).replace("import pytest\n\n", "").replace("from platform_common.errors import NotFound\n", "")

# Scenario 7's unrelated push: a docstring tidy-up in the org store.
S7_ORGS = FILES["platform_common/store/orgs.py"].replace(
    '''def get_org(org_id: int) -> Org:
    row = _TABLE.get(org_id)''',
    '''def get_org(org_id: int) -> Org:
    """Return the organisation with id `org_id`; raises NotFound if there is none."""
    row = _TABLE.get(org_id)''',
)
