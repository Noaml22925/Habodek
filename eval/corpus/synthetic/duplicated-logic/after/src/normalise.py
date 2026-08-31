def normalise_customer(record: dict) -> dict:
    name = record.get("name", "")
    name = name.strip().lower()
    name = name.replace("  ", " ")
    name = name.removesuffix(" ltd").removesuffix(" inc")
    return {**record, "name": name}


def normalise_supplier(record: dict) -> dict:
    name = record.get("company", "")
    name = name.strip().lower()
    name = name.replace("  ", " ")
    name = name.removesuffix(" ltd").removesuffix(" inc")
    return {**record, "company": name}
