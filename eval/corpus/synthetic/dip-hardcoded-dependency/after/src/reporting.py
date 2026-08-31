from infra.postgres import PostgresConnection


class ReportService:
    def __init__(self):
        self._connection = PostgresConnection(host="prod-db-01", port=5432)

    def monthly_totals(self) -> list[dict]:
        return self._connection.query("SELECT month, total FROM revenue")
