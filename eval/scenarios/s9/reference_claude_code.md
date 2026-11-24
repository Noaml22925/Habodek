# S9 — Claude Code reference: thread on F2 after "ownership is enforced by middleware"

Event: reply — "Ownership is enforced by the owns_resource middleware, it is
registered for this route."

Checked the claim: `owns_refund` exists (`orders/middleware/ownership.py:28`), but it
is registered only as `router.use_prefix("/api/v2/refunds", owns_refund)`
(`orders/app.py:16`). The route in question is `/api/v1/refunds/{refund_id}`
(`orders/api/refunds.py:16`), registered by `router.register(refunds.bp)`
(`orders/app.py:20`) with no blueprint middleware. `Router._chain` only applies prefix
middleware whose prefix matches the route pattern, so the v1 route runs only the
`/api` middleware (`request_id`, `access_log`) — not even authentication. Any caller,
logged in or not, gets any refund.

## Verdict: `stands`

> This still applies. `owns_refund` is registered only for `/api/v2/refunds`
> (`orders/app.py:16`); this handler is on the v1 blueprint (`orders/app.py:20`,
> route `/api/v1/refunds/{refund_id}`), so the router never runs it here.
> `GET /api/v1/refunds/<any id>` returns the refund to any caller
> (`orders/api/refunds.py:18-21`). Registering it for v1 too —
> `router.use_prefix("/api/v1/refunds", owns_refund)` — or checking
> `refund.user_id == request.user_id` in the handler would fix it.
