# S4 — Claude Code reference review

MR `orders-service`: *Admin: list all refunds* (`GET /admin/refunds`).

What I did: the handler has no auth check of its own, so I traced how `/admin` routes
are protected: `orders/api/admin.py` registers it on `admin = AdminBlueprint()`;
`platform-common` 2.3.0 `platform_common/web/admin.py:16-21` — `AdminBlueprint.__init__`
calls `self.use(require_user)` and `self.use(require_admin)`;
`platform_common/web/router.py` `Router.register` copies `blueprint.middleware` into
every route's chain and `dispatch` runs the chain before the handler;
`orders/app.py:20` registers `admin.admin`. `require_admin` (`web/auth.py`) raises
`Forbidden` for non-admins. The new route is under that chain like `/admin/users`.
The response exposes refund fields only (no card data).

## Comments

None.

## Summary

No issues found. The new endpoint is protected by the admin middleware that
`AdminBlueprint` registers on every route (checked in the `platform-common` 2.3.0
source pinned by this repository).
