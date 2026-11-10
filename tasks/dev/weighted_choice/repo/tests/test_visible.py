import pytest

from randomkit import choose_weighted


def test_zero_weight_and_boundaries() -> None:
    items = ["never", "a", "b"]
    weights = [0, 2, 3]
    assert choose_weighted(items, weights, 0) == "a"
    assert choose_weighted(items, weights, 1.999) == "a"
    assert choose_weighted(items, weights, 2) == "b"


@pytest.mark.parametrize(
    ("items", "weights", "draw"),
    [([], [], 0), (["a"], [0], 0), (["a"], [-1], 0), (["a"], [1], 1)],
)
def test_invalid_inputs_raise(items, weights, draw) -> None:
    with pytest.raises(ValueError):
        choose_weighted(items, weights, draw)
