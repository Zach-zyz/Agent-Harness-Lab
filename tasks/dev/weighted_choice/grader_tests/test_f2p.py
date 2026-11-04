import pytest

from randomkit import choose_weighted


def test_multiple_zero_weight_items_are_skipped() -> None:
    assert choose_weighted(["x", "y", "z"], [0.0, 0, 1], 0.0) == "z"


@pytest.mark.parametrize(
    ("items", "weights", "draw"),
    [
        (["a"], [True], 0),
        (["a"], ["1"], 0),
        (["a", "b"], [1], 0),
        (["a"], [1], -0.1),
        (["a"], [1], "0"),
    ],
)
def test_validation_is_strict(items, weights, draw) -> None:
    with pytest.raises(ValueError):
        choose_weighted(items, weights, draw)
