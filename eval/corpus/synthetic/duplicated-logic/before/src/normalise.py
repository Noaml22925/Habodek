def normalise_customer(record: dict) -> dict:
    name = record.get("name", "")
    name = name.strip().lower()
    name = name.replace("  ", " ")
    name = name.removesuffix(" ltd").removesuffix(" inc")
    return {**record, "name": name}
