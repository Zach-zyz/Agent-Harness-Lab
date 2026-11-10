from cachekit import TTLCache


def test_expired_entry_is_deleted() -> None:
    now = [0.0]
    cache = TTLCache(clock=lambda: now[0])
    cache.set("token", "secret", ttl=1.5)
    now[0] = 1.5
    assert cache.get("token", "gone") == "gone"
    assert "token" not in cache._values


def test_fractional_ttl_before_boundary() -> None:
    now = [2.0]
    cache = TTLCache(clock=lambda: now[0])
    cache.set("key", 3, ttl=0.25)
    now[0] = 2.249
    assert cache.get("key") == 3
