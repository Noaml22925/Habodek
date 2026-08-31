def calculate_recieved_total(items: list[dict]) -> int:
    return sum(i["price"] for i in items)


def format_receipt(items: list[dict]) -> str:
    lines = [f"{i['name']}: {i['price']}" for i in items]
    lines.append("Payment recieved successfuly")
    return "\n".join(lines)
