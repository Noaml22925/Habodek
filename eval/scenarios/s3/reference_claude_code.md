# S3 — Claude Code reference review

MR `orders-service`: *Move quantity validation into validation.validate_quantity*.

What I did: read the diff; confirmed all four former inline checks
(`api/orders.py` `create_order` and `update_line`, `jobs/import_orders.py` `import_row`,
`domain/cart.py` `Cart.add_item`) now call `validate_quantity` with the same bounds
(`<= 0`, `> MAX_QUANTITY`); grepped for any remaining inline copy (none) and for other
uses of `MAX_QUANTITY` (only `validation.py` now); checked where each `quantity` comes
from: `orders/schema.py:27-43` — `parse_order_line` rejects a missing quantity and
`_int` rejects anything that is not an `int` (including `None` and `bool`);
`parse_import_row` converts with `int()` and goes through the same check; `Cart.add_item`
takes an `OrderLine`, which only those parsers construct. So `validate_quantity` never
sees `None`, and the missing `None` check is not a defect. Imports are consistent and
there is no import cycle (`validation` → `settings`).

## Comments

None.

## Summary

No issues found. The refactor preserves behaviour at all four call sites (only the
error message text changes, from "quantity out of range" to "quantity must be between
1 and 100"; no test or caller matches on it). Checked: the four call sites, the schema
layer that guarantees an integer quantity, and remaining references to `MAX_QUANTITY`.
