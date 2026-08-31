class Account:
    def __init__(self, balance: int = 0):
        self.balance = balance

    def deposit(self, amount: int) -> None:
        self.balance += amount

    def withdraw(self, amount: int) -> None:
        if amount > self.balance:
            raise ValueError("insufficient funds")
        self.balance -= amount


class ReadOnlyAccount(Account):
    def deposit(self, amount: int) -> None:
        raise NotImplementedError("read-only account")

    def withdraw(self, amount: int) -> None:
        raise NotImplementedError("read-only account")


def drain(account: Account) -> int:
    """Withdraw everything and return the amount taken."""
    amount = account.balance
    account.withdraw(amount)
    return amount
