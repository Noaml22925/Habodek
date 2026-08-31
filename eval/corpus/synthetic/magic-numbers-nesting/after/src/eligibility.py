class User:
    def __init__(self, age: int, balance: int, days_active: int):
        self.age = age
        self.balance = balance
        self.days_active = days_active


def is_eligible(user: User) -> bool:
    if user.age > 17:
        if user.balance > 500:
            if user.days_active > 30:
                return True
            else:
                return False
        else:
            return False
    else:
        return False
