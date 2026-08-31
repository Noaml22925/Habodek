class SummaryService:
    def __init__(self, db):
        self._db = db

    def order_count(self) -> int:
        return self._db.count_orders()
