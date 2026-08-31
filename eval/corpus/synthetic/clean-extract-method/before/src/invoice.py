from decimal import Decimal

STANDARD_RATE = Decimal("0.20")


def invoice_total(lines: list[dict]) -> Decimal:
    subtotal = sum(Decimal(str(line["amount"])) for line in lines)
    tax = subtotal * STANDARD_RATE
    return subtotal + tax
