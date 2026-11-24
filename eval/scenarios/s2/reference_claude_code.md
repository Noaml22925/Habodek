# S2 — Claude Code reference review

MR `orders-service`: *Bump payments-lib to 1.5.0* (`requirements.txt` only).

What I did: diffed `payments-lib` 1.4.2 against 1.5.0 (`client.py`, `models.py`,
`CHANGELOG.md`); found every use of the payments API in `orders-service`
(`orders/domain/checkout.py` is the only caller of `charge`); read `checkout`,
`fulfilment.enqueue` and the tests.

## Comment on `requirements.txt:2` (`payments-lib==1.5.0`)

**payments-lib 1.5.0 no longer raises `PaymentDeclined` — a declined charge comes back as a result — and checkout marks declined orders as paid.**

- `payments-lib` 1.5.0 `payments/client.py:31-33`: on `status == "declined"` it now
  `return ChargeResult(..., "declined", ..., decline_reason=...)` instead of
  `raise PaymentDeclined(...)` (CHANGELOG 1.5.0: "No exceptions for control flow").
- `orders/domain/checkout.py:18-25`: the only decline handling is
  `except PaymentDeclined`, which can no longer happen.
- `orders/domain/checkout.py:26-29`: so a declined charge falls through to
  `order.status = "paid"`, stores the (empty) `charge_id`, and
  `fulfilment.enqueue(order)` ships it — goods go out for payments that were declined.
- `tests/test_checkout.py::test_declined_cancels` fails against 1.5.0, which confirms it.

**Fix:** in the same MR, branch on the result:
`if not result.succeeded: order.status = "cancelled"; order.cancel_reason = result.decline_reason; …`
(keep the `except` only if you must support both versions).

## Summary

One blocking issue: the version bump changes how declines are reported, and checkout
treats every declined charge as paid. Checked: the full library diff between the two
versions, every use of `payments` in this repository, and the checkout tests.
