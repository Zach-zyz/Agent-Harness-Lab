from randomkit import choose_weighted


def test_regular_weighted_intervals_still_work() -> None:
    items = ["a", "b", "c"]
    weights = [1, 2, 1]
    assert choose_weighted(items, weights, 0.5) == "a"
    assert choose_weighted(items, weights, 1.5) == "b"
    assert choose_weighted(items, weights, 3.5) == "c"
