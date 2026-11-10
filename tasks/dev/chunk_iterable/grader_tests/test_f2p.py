from itertools import count, islice

from iterkit import chunked


def test_chunking_is_lazy_for_unbounded_iterables() -> None:
    batches = chunked(count(), 3)
    assert list(islice(batches, 2)) == [[0, 1, 2], [3, 4, 5]]


def test_empty_iterable_has_no_empty_chunk() -> None:
    assert list(chunked(iter(()), 4)) == []
