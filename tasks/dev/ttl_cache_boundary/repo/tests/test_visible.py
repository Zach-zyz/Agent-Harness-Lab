import pytest

from cachekit import TTLCache


def test_zero_ttl_expires_immediately() -> None:
    cache = TTLCache(clock=lambda: 10.0)
    cache.set("key", "value", ttl=0)
    assert cache.get("key") is None


def test_exact_expiration_boundary_is_expired() -> None:
    now = [5.0]
    cache = TTLCache(clock=lambda: now[0])
    cache.set("key", "value", ttl=2)
    now[0] = 7.0
    assert cache.get("key", "missing") == "missing"


@pytest.mark.parametrize("ttl", [-1, True, "1"])
def test_invalid_ttl_is_rejected(ttl) -> None:
    with pytest.raises(ValueError):
        TTLCache(clock=lambda: 0).set("key", "value", ttl=ttl)
