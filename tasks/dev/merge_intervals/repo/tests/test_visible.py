import pytest

from rangekit import merge_intervals


def test_touching_intervals_are_merged() -> None:
    assert merge_intervals([(5, 7), (1, 3), (3, 5)]) == [[1, 7]]


def test_reversed_interval_is_rejected() -> None:
    with pytest.raises(ValueError):
        merge_intervals([(3, 2)])
