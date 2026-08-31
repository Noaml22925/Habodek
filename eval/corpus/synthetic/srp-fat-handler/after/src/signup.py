class SignupHandler:
    def __init__(self, db, mailer):
        self._db = db
        self._mailer = mailer

    def handle(self, raw_body: bytes) -> dict:
        import json

        payload = json.loads(raw_body.decode("utf-8"))
        email = payload.get("email", "").strip().lower()
        if "@" not in email:
            return {"status": 400, "error": "bad email"}
        if len(payload.get("password", "")) < 8:
            return {"status": 400, "error": "weak password"}
        existing = self._db.execute(
            "SELECT id FROM users WHERE email = %s", (email,)
        ).fetchone()
        if existing:
            return {"status": 409, "error": "exists"}
        user_id = self._db.execute(
            "INSERT INTO users (email, password) VALUES (%s, %s) RETURNING id",
            (email, payload["password"]),
        ).fetchone()[0]
        body = f"Welcome {email}, your account is ready."
        self._mailer.send(to=email, subject="Welcome", body=body)
        return {"status": 201, "id": user_id}
