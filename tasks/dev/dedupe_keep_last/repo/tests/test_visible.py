from recordkit import dedupe_keep_last


def test_survivors_follow_last_occurrence_order() -> None:
    records = [
        {"id": "a", "value": 1},
        {"id": "b", "value": 2},
        {"id": "a", "value": 3},
    ]
    assert dedupe_keep_last(records, key=lambda item: item["id"]) == [
        {"id": "b", "value": 2},
        {"id": "a", "value": 3},
    ]
