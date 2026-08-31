from src.cart import set_quantity


def test_sets_quantity():
    assert set_quantity({}, "abc", 2) == {"abc": 2}
