from headerkit import get_header


def test_exact_match_default_and_mapping_are_preserved() -> None:
    headers = {"Accept": "text/plain", 7: "numeric-key"}
    snapshot = dict(headers)
    assert get_header(headers, "Accept") == "text/plain"
    assert get_header(headers, "Missing", "fallback") == "fallback"
    assert headers == snapshot
