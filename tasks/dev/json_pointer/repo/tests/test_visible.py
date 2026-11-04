import pytest

from pointerkit import resolve_pointer


def test_escaped_keys_and_array_indices() -> None:
    document = {"a/b": {"~key": [{"value": 3}]}}
    assert resolve_pointer(document, "/a~1b/~0key/0/value") == 3


@pytest.mark.parametrize("pointer", ["missing", "/items/01", "/items/-1", "/items/9"])
def test_invalid_pointers_raise_key_error(pointer) -> None:
    with pytest.raises(KeyError) as error:
        resolve_pointer({"items": ["a"]}, pointer)
    assert error.value.args == (pointer,)
