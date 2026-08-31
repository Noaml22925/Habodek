class OrderRepository:
    def __init__(self, db):
        self._db = db

    def all_orders(self) -> list[dict]:
        return self._db.execute("SELECT * FROM orders").fetchall()
