from decimal import Decimal

STANDARD_RATE = Decimal("0.20")


def subtotal_of(lines: list[dict]) -> Decimal:
    return sum(Decimal(str(line["amount"])) for line in lines)


def tax_on(subtotal: Decimal) -> Decimal:
    return subtotal * STANDARD_RATE


def invoice_total(lines: list[dict]) -> Decimal:
    subtotal = subtotal_of(lines)
    return subtotal + tax_on(subtotal)
