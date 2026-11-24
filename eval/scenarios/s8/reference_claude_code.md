# S8 — Claude Code reference: thread on F2 after "ownership is enforced by middleware"

Event: reply — "Ownership is enforced by the owns_resource middleware, it is
registered for this route."

Checked the claim: `orders/app.py:17` registers
`router.use_prefix("/api/v1/refunds", owns_refund)`. `Router._chain`
(`platform_common/web/router.py`) applies prefix middleware to every route whose
pattern equals the prefix or starts with `prefix + "/"`; the route is
`/api/v1/refunds/{refund_id}` (`orders/api/refunds.py:16`, blueprint prefix `/api/v1`),
so it is covered. `owns_refund` (`orders/middleware/ownership.py:13-28`) loads the
refund by `refund_id` and raises `Forbidden` unless `resource.user_id ==
request.user_id`, before the handler runs. That is every path to `get_refund`. (The
other v1 refunds route, `POST /api/v1/orders/{order_id}/refunds`, is not under the
prefix and checks order ownership inline.)

## Verdict: `withdrawn`

> You're right — withdrawn. `owns_refund` is registered on the `/api/v1/refunds`
> prefix (`orders/app.py:17`), the router applies it to
> `GET /api/v1/refunds/{refund_id}`, and it rejects any caller who does not own the
> refund (`orders/middleware/ownership.py:20-21`) before `get_refund` runs. I missed
> the prefix registration.

Lesson candidate: in orders-service, route-level ownership checks can live in
`orders/app.py` as `router.use_prefix(..., owns_*)`; check prefix registrations before
reporting a missing ownership check in a handler.
