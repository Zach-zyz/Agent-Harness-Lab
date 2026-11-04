from rangekit import merge_intervals


def test_disjoint_overlap_empty_and_input_preservation() -> None:
    intervals = [(5, 8), (1, 3), (2, 4), (10, 11)]
    snapshot = list(intervals)
    assert merge_intervals(intervals) == [[1, 4], [5, 8], [10, 11]]
    assert intervals == snapshot
    assert merge_intervals([]) == []
