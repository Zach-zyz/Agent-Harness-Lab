from querykit import parse_query


def test_repeated_and_blank_values_are_preserved() -> None:
    assert parse_query("tag=red&empty=&tag=blue") == {
        "tag": ["red", "blue"],
        "empty": [""],
    }


def test_empty_query() -> None:
    assert parse_query("") == {}
