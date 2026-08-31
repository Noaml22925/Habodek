class ReportService:
    def __init__(self, connection):
        self._connection = connection

    def monthly_totals(self) -> list[dict]:
        return self._connection.query("SELECT month, total FROM revenue")
