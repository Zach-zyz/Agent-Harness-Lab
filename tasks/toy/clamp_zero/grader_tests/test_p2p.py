import pytest

from numkit import clamp


def test_nonzero_and_missing_bounds_still_work():
    assert clamp(5, lower=1, upper=4) == 4
    assert clamp(-2, lower=-1) == -1
    assert clamp(3) == 3


def test_reversed_bounds_still_raise():
    with pytest.raises(ValueError):
        clamp(0, lower=2, upper=1)
