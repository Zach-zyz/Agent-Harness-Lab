import pytest

from pointerkit import resolve_pointer


def test_empty_key_and_escape_order() -> None:
    document = {"": 1, "~1": 2, "/": 3}
    assert resolve_pointer(document, "/") == 1
    assert resolve_pointer(document, "/~01") == 2
    assert resolve_pointer(document, "/~1") == 3


@pytest.mark.parametrize("pointer", ["/items/-", "/items/x", "/scalar/next"])
def test_invalid_container_or_index_is_key_error(pointer) -> None:
    document = {"items": [10], "scalar": 1}
    with pytest.raises(KeyError) as error:
        resolve_pointer(document, pointer)
    assert error.value.args == (pointer,)
