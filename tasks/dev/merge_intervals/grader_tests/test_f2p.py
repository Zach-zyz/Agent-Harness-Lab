import pytest

from rangekit import merge_intervals


def test_touching_chain_and_contained_intervals() -> None:
    assert merge_intervals([(8, 10), (1, 4), (2, 3), (4, 8)]) == [[1, 10]]


def test_invalid_interval_anywhere_is_rejected() -> None:
    with pytest.raises(ValueError):
        merge_intervals([(0, 1), (9, 2), (3, 4)])
