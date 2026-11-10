from iterkit import chunked


def test_sequence_behavior_and_independent_lists() -> None:
    result = list(chunked([1, 2, 3, 4], 2))
    assert result == [[1, 2], [3, 4]]
    result[0].append(9)
    assert result[1] == [3, 4]
