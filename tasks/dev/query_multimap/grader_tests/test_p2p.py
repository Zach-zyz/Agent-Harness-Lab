from querykit import parse_query


def test_single_values_and_percent_decoding_still_work() -> None:
    assert parse_query("name=Ada&path=a%2Fb") == {
        "name": ["Ada"],
        "path": ["a/b"],
    }
    assert parse_query("") == {}
