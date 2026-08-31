def register_webhook(url: str, events: list[str]) -> dict:
    if not url.startswith("https://"):
        raise ValueError("webhook url must be https")
    return {"url": url, "events": events}
