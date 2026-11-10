import pytest

from iterkit import chunked


def test_generator_input_is_consumed_in_chunks() -> None:
    source = (value for value in range(5))
    assert list(chunked(source, 2)) == [[0, 1], [2, 3], [4]]


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_invalid_sizes_raise(size) -> None:
    with pytest.raises(ValueError):
        list(chunked([1, 2], size))
