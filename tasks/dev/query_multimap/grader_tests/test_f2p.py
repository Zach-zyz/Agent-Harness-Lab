from querykit import parse_query


def test_decoding_and_encounter_order() -> None:
    result = parse_query("q=hello+world&q=%E2%9C%93&flag=&q=last")
    assert list(result) == ["q", "flag"]
    assert result == {"q": ["hello world", "✓", "last"], "flag": [""]}


def test_key_without_equals_has_blank_value() -> None:
    assert parse_query("debug&debug=1") == {"debug": ["", "1"]}
