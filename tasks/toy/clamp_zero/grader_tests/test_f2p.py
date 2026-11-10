from numkit import clamp


def test_zero_lower_and_upper_bounds():
    assert clamp(-3, lower=0) == 0
    assert clamp(3, upper=0) == 0
    assert clamp(0, lower=0, upper=0) == 0
