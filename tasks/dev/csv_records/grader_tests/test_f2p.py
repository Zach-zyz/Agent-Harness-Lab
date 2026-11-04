import pytest

from csvkit import read_records


def test_multiline_quoted_field_and_empty_field() -> None:
    text = 'id,description,tag\n1,"first line\nsecond line",\n'
    assert read_records(text) == [
        {"id": "1", "description": "first line\nsecond line", "tag": ""}
    ]


def test_extra_column_is_rejected() -> None:
    with pytest.raises(ValueError):
        read_records("a,b\n1,2,3\n")
