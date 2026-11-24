# S5 — Claude Code reference: thread on F1 after a push

Event: push to `mr/s5-restore-raise`. Diff since the finding:
`platform_common/store/users.py` — `get_user` raises `NotFound(f"user {uid}")` again
(line 30) and the return type is `User` again; `delete_user`'s added `None` check is
gone; `tests/test_users.py` and `tests/test_cache.py` are back to asserting `NotFound`
and the negative cache.

Re-walked the chain at the new head: `cache/users.py:25-29` catches `NotFound` and
records the miss again; `orders-service` `orders/api/profile.py:16` gets `NotFound`
(router → 404) before `.email`; `orders/jobs/digest.py:17-21` removes the deleted
subscriber and continues.

## Verdict: `fixed`

> Fixed: `get_user` raises `NotFound` again (`platform_common/store/users.py:30`), so
> `CachedUsers.get` negative-caches misses again and the `orders-service` callers get
> the 404 / skip-deleted-subscriber behaviour they were written for. The tests assert
> it again (`tests/test_users.py`, `tests/test_cache.py`).
