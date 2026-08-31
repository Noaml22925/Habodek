def register_webhook(url: str, events: list[str] = []) -> dict:
    if not url.startswith("https://"):
        raise ValueError("webhook url must be https")
    events.append("ping")
    return {"url": url, "events": events}
