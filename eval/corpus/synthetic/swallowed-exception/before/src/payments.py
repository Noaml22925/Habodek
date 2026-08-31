from decimal import Decimal


class PaymentGateway:
    """Reads charge history from the upstream payment provider."""

    def __init__(self, client):
        self._client = client

    def charges_for(self, customer_id: str) -> list[dict]:
        response = self._client.get(f"/customers/{customer_id}/charges")
        return response["data"]

    def total_charged(self, customer_id: str) -> Decimal:
        return sum(Decimal(c["amount"]) for c in self.charges_for(customer_id))
