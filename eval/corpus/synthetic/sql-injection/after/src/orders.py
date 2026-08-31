class OrderRepository:
    def __init__(self, db):
        self._db = db

    def all_orders(self) -> list[dict]:
        return self._db.execute("SELECT * FROM orders").fetchall()

    def find_by_status(self, status: str) -> list[dict]:
        query = f"SELECT * FROM orders WHERE status = '{status}'"
        return self._db.execute(query).fetchall()
