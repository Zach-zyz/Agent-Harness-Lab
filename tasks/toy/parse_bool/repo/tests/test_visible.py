import pytest

from boolkit import parse_bool


def test_documented_string_forms() -> None:
    for value in ["true", " TRUE ", "yes", "Yes", "1"]:
        assert parse_bool(value) is True
    for value in ["false", " FALSE ", "no", "No", "0"]:
        assert parse_bool(value) is False


def test_bool_inputs_are_unchanged() -> None:
    assert parse_bool(True) is True
    assert parse_bool(False) is False


def test_invalid_values_raise_value_error() -> None:
    for value in ["", "off", "truthy", 2, None]:
        with pytest.raises(ValueError):
            parse_bool(value)
