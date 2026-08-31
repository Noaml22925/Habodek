import pytest

from src.cart import set_quantity


def test_sets_quantity():
    assert set_quantity({}, "abc", 2) == {"abc": 2}


def test_rejects_negative_quantity():
    with pytest.raises(ValueError, match="must not be negative"):
        set_quantity({}, "abc", -1)
