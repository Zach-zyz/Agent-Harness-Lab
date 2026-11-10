from cachekit import TTLCache


def test_non_expiring_missing_and_overwrite_behavior() -> None:
    now = [100.0]
    cache = TTLCache(clock=lambda: now[0])
    cache.set("key", "first")
    now[0] = 1000.0
    assert cache.get("key") == "first"
    assert cache.get("missing", 7) == 7
    cache.set("key", "second", ttl=None)
    assert cache.get("key") == "second"
