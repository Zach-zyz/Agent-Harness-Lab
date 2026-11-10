import pytest

from boolkit import parse_bool


def test_all_documented_false_forms():
    for value in ["false", " FALSE ", "no", "No", "0"]:
        assert parse_bool(value) is False


def test_invalid_values_raise():
    for value in ["", "off", "truthy", 2, None]:
        with pytest.raises(ValueError):
            parse_bool(value)
