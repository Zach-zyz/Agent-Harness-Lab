from recordkit import dedupe_keep_last


def test_unique_and_empty_inputs_preserve_order() -> None:
    assert dedupe_keep_last(["a", "b", "c"]) == ["a", "b", "c"]
    assert dedupe_keep_last([]) == []
