def page(items: list, page_number: int, per_page: int) -> list:
    """Return one page of items. `page_number` is one-based."""
    start = (page_number - 1) * per_page
    end = start + per_page + 1
    return items[start:end]
