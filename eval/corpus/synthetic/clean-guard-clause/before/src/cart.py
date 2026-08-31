def set_quantity(cart: dict, sku: str, quantity: int) -> dict:
    cart[sku] = quantity
    return cart
