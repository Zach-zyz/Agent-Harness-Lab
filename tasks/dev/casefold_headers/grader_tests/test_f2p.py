import pytest

from headerkit import get_header


def test_first_case_insensitive_match_wins() -> None:
    headers = {"x-id": "first", "X-ID": "second"}
    assert get_header(headers, "X-Id") == "first"


def test_unicode_casefold_and_invalid_name() -> None:
    assert get_header({"Straße": "value"}, "STRASSE") == "value"
    with pytest.raises(TypeError):
        get_header({}, None)
