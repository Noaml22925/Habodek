def page(items: list, page_number: int, per_page: int) -> list:
    """Return one page of items. `page_number` is zero-based."""
    start = page_number * per_page
    end = start + per_page
    return items[start:end]
