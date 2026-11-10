from pointerkit import resolve_pointer


def test_root_and_simple_dictionary_paths_still_work() -> None:
    document = {"user": {"name": "Ada"}}
    assert resolve_pointer(document, "") is document
    assert resolve_pointer(document, "/user/name") == "Ada"
