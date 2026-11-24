# S7 — Claude Code reference: thread on F1 after "fixed"

Event: reply "fixed", with a push. Diff since the finding: one line in
`platform_common/store/orgs.py` (a docstring on `get_org`). `get_user` is unchanged.

Re-walked the chain at the new head: `platform_common/store/users.py:28` still
`return None`; `cache/users.py:27` still only negative-caches on `NotFound`;
`orders-service` `orders/api/profile.py:17` and `orders/jobs/digest.py:22` still
dereference the result.

## Verdict: `stands`

> This still applies: the push only adds a docstring to `get_org`
> (`platform_common/store/orgs.py`). `get_user` still returns `None` for a missing user
> (`platform_common/store/users.py:28`), so a deleted user's `GET /api/v1/profile` in
> orders-service still fails with `AttributeError` at `orders/api/profile.py:17`
> (500 instead of 404), the digest still dies at `orders/jobs/digest.py:22`, and
> `cache/users.py:27` still never caches a miss. Was a different commit meant to be
> pushed?
