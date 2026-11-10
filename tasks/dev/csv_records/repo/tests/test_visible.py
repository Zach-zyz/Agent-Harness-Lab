import pytest

from csvkit import read_records


def test_quoted_fields_and_blank_lines() -> None:
    text = 'name,note\nAda,"hello, world"\n\nGrace,"line ""quoted"""\n'
    assert read_records(text) == [
        {"name": "Ada", "note": "hello, world"},
        {"name": "Grace", "note": 'line "quoted"'},
    ]


@pytest.mark.parametrize("text", ["a,a\n1,2\n", "a,b\n1\n", ",b\n1,2\n"])
def test_invalid_shape_or_header_raises(text) -> None:
    with pytest.raises(ValueError):
        read_records(text)
