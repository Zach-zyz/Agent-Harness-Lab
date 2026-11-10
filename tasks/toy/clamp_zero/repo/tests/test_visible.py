import pytest

from numkit import clamp


def test_zero_bounds_are_respected() -> None:
    assert clamp(-5, lower=0) == 0
    assert clamp(5, upper=0) == 0
    assert clamp(0, lower=0, upper=0) == 0


def test_nonzero_and_missing_bounds() -> None:
    assert clamp(5, lower=1, upper=4) == 4
    assert clamp(-2, lower=-1) == -1
    assert clamp(3) == 3


def test_reversed_bounds_raise() -> None:
    with pytest.raises(ValueError):
        clamp(0, lower=2, upper=1)
