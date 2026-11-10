from datetime import datetime, timezone

from httpkit import parse_retry_after


def test_regular_delta_seconds_and_exact_date() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert parse_retry_after("0", now) == 0
    assert parse_retry_after("15", now) == 15
    assert parse_retry_after("Thu, 01 Jan 2026 00:00:05 GMT", now) == 5
