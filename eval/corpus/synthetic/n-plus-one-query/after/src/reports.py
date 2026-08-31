class SummaryService:
    def __init__(self, db):
        self._db = db

    def order_count(self) -> int:
        return self._db.count_orders()

    def order_summaries(self, order_ids: list[int]) -> list[dict]:
        summaries = []
        for order_id in order_ids:
            order = self._db.get_order(order_id)
            customer = self._db.get_customer(order["customer_id"])
            summaries.append({"order": order_id, "customer": customer["name"]})
        return summaries
