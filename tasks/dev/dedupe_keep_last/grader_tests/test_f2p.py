from recordkit import dedupe_keep_last


def test_generator_and_multiple_duplicate_groups() -> None:
    source = (item for item in ["a1", "b1", "a2", "c1", "b2"])
    assert dedupe_keep_last(source, key=lambda item: item[0]) == ["a2", "c1", "b2"]


def test_key_is_called_once_per_item() -> None:
    calls = []

    def key(item):
        calls.append(item)
        return item % 2

    assert dedupe_keep_last([1, 2, 3], key=key) == [2, 3]
    assert calls == [1, 2, 3]
