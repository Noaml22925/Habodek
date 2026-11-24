"""The nine scenarios (handoff §9): what each MR or thread event is, and what is expected.

Expected sites name a file and a line *text*; `build.py` resolves line numbers from the
generated repositories, so editing a repository cannot silently desynchronise them.
A site's `repo` is a repository name ("orders-service") or a dependency
("payments-lib"); no `repo` means the reviewed repository.
"""

from __future__ import annotations

INTERNAL_PACKAGES = ["platform-common", "payments-lib"]

F1 = {
    "id": "F1",
    "repo": "platform-common",
    "made_at": "mr/s1-get-user-none",
    "title": "get_user() now returns None for a missing user, and callers dereference it",
    "severity": "major",
    "category": "contract",
    "claim": "get_user() returns None instead of raising NotFound for a missing or deleted "
             "user. CachedUsers.get only negative-caches on NotFound, and consumers that "
             "dereference the result (orders-service profile endpoint and weekly digest) "
             "now raise AttributeError.",
    "scenario": "A deleted user calls GET /api/v1/profile: get_user returns None and "
                "user.email raises AttributeError, served as a 500 where it was a 404.",
    "fix": "Keep raising NotFound in get_user, or update every caller to handle None.",
    "anchor": {"path": "platform_common/store/users.py", "text": "return None"},
    "chain": [
        {"path": "platform_common/store/users.py", "text": "return None",
         "claim": "returns None where it raised NotFound"},
        {"path": "platform_common/cache/users.py", "text": "except NotFound:",
         "claim": "negative caching only happens on NotFound, so misses are no longer cached"},
        {"repo": "orders-service", "path": "orders/api/profile.py", "text": "user.email",
         "claim": "dereferences the result: AttributeError, 500 instead of 404"},
        {"repo": "orders-service", "path": "orders/jobs/digest.py", "text": "user.email",
         "claim": "the NotFound handler no longer fires; the digest dies on a deleted user"},
    ],
}

F2 = {
    "id": "F2",
    "repo": "orders-service",
    "title": "GET /api/v1/refunds/{refund_id} returns any user's refund (no ownership check)",
    "severity": "critical",
    "category": "security",
    "claim": "get_refund loads the refund by id and returns it without checking that the "
             "caller owns it: an insecure direct object reference.",
    "scenario": "User 2 requests /api/v1/refunds/<id of user 1's refund> and receives it.",
    "fix": "Check refund.user_id == request.user_id, or register the owns_refund "
           "middleware on the v1 refunds prefix.",
    "anchor": {"path": "orders/api/refunds.py",
               "text": 'refund = refunds_store.get(request.params["refund_id"])'},
    "chain": [
        {"path": "orders/api/refunds.py",
         "text": 'refund = refunds_store.get(request.params["refund_id"])',
         "claim": "loads any refund by id"},
        {"path": "orders/api/refunds.py", "text": "return Response(200, refund.as_dict())",
         "claim": "returns it without an ownership check"},
        {"path": "orders/app.py", "text": "router.register(refunds.bp)",
         "claim": "the v1 blueprint is registered with no ownership middleware"},
    ],
}

SCENARIOS: dict[str, dict] = {
    "1": {
        "id": "S1", "name": "cross-lib", "kind": "review", "repo": "platform-common",
        "base": "v2.3.0", "head": "mr/s1-get-user-none",
        "mr": {"title": "Return None from get_user for missing users",
               "description": "Raising for a missing user forces every caller into "
                              "try/except. Return None instead, like dict.get. Tests "
                              "updated.", "author": "dev-a"},
        "other_repos": {"orders-service": "main"},
        "expect": {
            "outcome": "found",
            "anchor": {"path": "platform_common/store/users.py", "text": "return None"},
            "sites": [
                {"path": "platform_common/cache/users.py"},
                {"repo": "orders-service", "path": "orders/api/profile.py"},
                {"repo": "orders-service", "path": "orders/jobs/digest.py"},
            ],
            "max_comments": 1,
        },
    },
    "2": {
        "id": "S2", "name": "dep-bump", "kind": "review", "repo": "orders-service",
        "base": "main", "head": "mr/s2-payments-1.5.0",
        "mr": {"title": "Bump payments-lib to 1.5.0",
               "description": "Routine dependency update.", "author": "dev-b"},
        "expect": {
            "outcome": "found",
            "anchor": {"path": "requirements.txt", "text": "payments-lib==1.5.0"},
            "sites": [
                {"repo": "payments-lib", "path": "payments/client.py"},
                {"path": "orders/domain/checkout.py"},
            ],
            "max_comments": 1,
        },
    },
    "3": {
        "id": "S3", "name": "clean-refactor", "kind": "review", "repo": "orders-service",
        "base": "main", "head": "mr/s3-validate-quantity",
        "mr": {"title": "Move quantity validation into validation.validate_quantity",
               "description": "The same range check was copied in four places. One "
                              "helper, all four call sites updated.", "author": "dev-c"},
        "expect": {"outcome": "silent"},
    },
    "4": {
        "id": "S4", "name": "admin-auth", "kind": "review", "repo": "orders-service",
        "base": "main", "head": "mr/s4-admin-refunds",
        "mr": {"title": "Admin: list all refunds",
               "description": "Support needs to see every refund. Adds GET /admin/refunds.",
               "author": "dev-d"},
        "expect": {"outcome": "silent"},
    },
    "5": {
        "id": "S5", "name": "fixed", "kind": "thread", "repo": "platform-common",
        "finding": "F1", "head": "mr/s5-restore-raise", "event": "push",
        "other_repos": {"orders-service": "main"},
        "notes": [],
        "expect": {"verdict": "fixed"},
    },
    "6": {
        "id": "S6", "name": "fix-incomplete", "kind": "thread", "repo": "platform-common",
        "finding": "F1", "head": "mr/s1-get-user-none", "event": "reply",
        "other_repos": {"orders-service": "s6/profile-none-check"},
        "notes": [{"author": "dev-a", "body": "Handled on the consumer side: orders-service "
                   "now checks for None in the profile endpoint (orders-service!88, merged)."}],
        "expect": {"verdict": "fix_incomplete",
                   "sites": [{"repo": "orders-service", "path": "orders/jobs/digest.py"}]},
    },
    "7": {
        "id": "S7", "name": "claimed-fixed", "kind": "thread", "repo": "platform-common",
        "finding": "F1", "head": "mr/s7-unrelated", "event": "reply",
        "other_repos": {"orders-service": "main"},
        "notes": [{"author": "dev-a", "body": "fixed"}],
        "expect": {"verdict": "stands",
                   "sites": [{"path": "platform_common/store/users.py"}]},
    },
    "8": {
        "id": "S8", "name": "withdrawn", "kind": "thread", "repo": "orders-service",
        "finding": "F2", "head": "mr/s8-refunds-v1", "event": "reply",
        "notes": [{"author": "dev-e", "body": "Ownership is enforced by the owns_resource "
                   "middleware, it is registered for this route."}],
        "expect": {"verdict": "withdrawn", "lesson": True},
    },
    "9": {
        "id": "S9", "name": "middleware-v2-only", "kind": "thread", "repo": "orders-service",
        "finding": "F2", "head": "mr/s9-refunds-v1", "event": "reply",
        "notes": [{"author": "dev-e", "body": "Ownership is enforced by the owns_resource "
                   "middleware, it is registered for this route."}],
        "expect": {"verdict": "stands",
                   "sites": [{"path": "orders/app.py"}]},
    },
}

FINDINGS = {"F1": F1, "F2": F2}
# F2 is the finding made on the refunds-v1 branch of each thread scenario.
F2_MADE_AT = {"8": "mr/s8-refunds-v1", "9": "mr/s9-refunds-v1"}


def select(spec: str) -> list[str]:
    """'1-4' -> ['1', '2', '3', '4']; '5,7' -> ['5', '7']; 'all' -> every id."""
    if spec in ("all", "*", ""):
        return sorted(SCENARIOS, key=int)
    ids: list[str] = []
    for part in spec.split(","):
        part = part.strip().lstrip("Ss")
        if "-" in part:
            lo, hi = part.split("-")
            ids += [str(i) for i in range(int(lo), int(hi.lstrip("Ss")) + 1)]
        elif part:
            ids.append(part)
    unknown = [i for i in ids if i not in SCENARIOS]
    if unknown:
        raise ValueError(f"unknown scenarios: {unknown}")
    return ids
