# S1 — Claude Code reference review

MR `platform-common`: *Return None from get_user for missing users* (`v2.3.0...mr/s1-get-user-none`).

What I did: read the diff; read `store/users.py` in full at base and head; found every
caller of `get_user` in this repository (`cache/users.py:26`, `web/auth.py:17`,
`store/users.py:42`) and in the consumer `orders-service` (`orders/api/profile.py:16`,
`orders/jobs/digest.py:18`); read each enclosing function; read the test changes.

## Comment on `platform_common/store/users.py:28` (`return None`)

**`get_user()` now returns `None` for a missing or deleted user, and its callers rely on the `NotFound` it used to raise.**

This changes the function's contract (the README still documents "raises
`errors.NotFound` … Callers rely on this"), and every caller was written against the
old one:

- `platform_common/cache/users.py:25-29` — `CachedUsers.get` only records a miss in
  `_negative` inside `except NotFound:`. That branch can no longer run, so misses are
  never negatively cached: every lookup of a deleted account goes to the primary again,
  which is what the negative cache exists to prevent. `get` also returns `None` where its
  signature says `User`. (`tests/test_cache.py` was edited to assert `None`, and the
  assertion on `_negative` was dropped with it, so CI does not catch this.)
- `orders-service` `orders/api/profile.py:16-17` — `user = get_user(request.user_id)`
  then `user.email`: for a deleted user this is now `AttributeError`, which the router
  serves as a 500 (`platform_common/web/router.py` last-resort handler) where it used to
  be a 404.
- `orders-service` `orders/jobs/digest.py:17-22` — the `except NotFound:` that removes
  deleted subscribers no longer fires; `user.email` on line 22 raises `AttributeError`,
  and the weekly digest job dies at the first deleted subscriber, so nobody after them
  gets the email.
- Also in this repository: `web/auth.py:17` stores `None` in `request.user`, and
  `require_admin` (`web/auth.py:22-24`) then evaluates `None.is_admin` — admin routes
  answer 500 instead of 404 for a deleted user's token.

**Fix:** keep raising `NotFound` from `get_user` (add a separate `find_user()` that
returns `None` if callers want that shape), or update every caller listed above in the
same release and bump the major version.

## Summary

One blocking issue: the `get_user` contract change breaks the negative cache in this
library and two call sites in `orders-service`. Checked: the 5 call sites of `get_user`
in 2 repositories, the router's error mapping, and the edited tests.
