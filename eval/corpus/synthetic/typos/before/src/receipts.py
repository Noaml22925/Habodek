def format_receipt(items: list[dict]) -> str:
    lines = [f"{i['name']}: {i['price']}" for i in items]
    return "\n".join(lines)
