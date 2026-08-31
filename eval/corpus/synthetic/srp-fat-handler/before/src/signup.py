class SignupHandler:
    def __init__(self, db, mailer):
        self._db = db
        self._mailer = mailer
