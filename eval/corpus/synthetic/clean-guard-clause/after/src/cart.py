def set_quantity(cart: dict, sku: str, quantity: int) -> dict:
    if quantity < 0:
        raise ValueError("quantity must not be negative")
    cart[sku] = quantity
    return cart
