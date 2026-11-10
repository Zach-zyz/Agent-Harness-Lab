import pytest

from headerkit import get_header


def test_lookup_is_case_insensitive() -> None:
    headers = {"Content-Type": "application/json", "X-Trace-ID": "abc"}
    assert get_header(headers, "content-type") == "application/json"
    assert get_header(headers, "X-TRACE-id") == "abc"


def test_non_string_name_is_rejected() -> None:
    with pytest.raises(TypeError):
        get_header({"X": "1"}, 1)
