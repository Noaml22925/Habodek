# S6 — Claude Code reference: thread on F1 after "handled on the consumer side"

Event: reply — "orders-service now checks for None in the profile endpoint". Nothing
was pushed to the library: `get_user` still returns `None`
(`platform_common/store/users.py:28`).

Went where the developer pointed: `orders-service` `orders/api/profile.py:16-18` now
raises `NotFound` when `user is None` — that site is fixed. Re-walked the rest of the
chain: `orders-service` `orders/jobs/digest.py:17-22` is unchanged — `except NotFound`
still cannot fire and line 22 still dereferences `user.email`; and
`platform_common/cache/users.py:25-29` still never records a miss.

## Verdict: `fix_incomplete`

> Partly fixed: the profile endpoint now handles `None`
> (`orders-service` `orders/api/profile.py:17`). Still broken:
> - `orders-service` `orders/jobs/digest.py:22` — `user.email` on `None`; the digest
>   job still dies at the first deleted subscriber (its `except NotFound` on line 19
>   can no longer fire).
> - `platform_common/cache/users.py:27` — misses are still never negatively cached.
>
> Restoring `raise NotFound` in `get_user` fixes all three at once.
