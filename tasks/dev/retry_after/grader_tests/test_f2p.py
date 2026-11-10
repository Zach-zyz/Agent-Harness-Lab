from datetime import datetime, timedelta, timezone

import pytest

from httpkit import parse_retry_after


def test_timezone_offsets_and_ceiling() -> None:
    now = datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=timezone.utc)
    assert parse_retry_after("Thu, 01 Jan 2026 01:00:01 +0100", now) == 1


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_retry_after("1", datetime(2026, 1, 1))


def test_large_decimal_seconds_are_preserved() -> None:
    assert parse_retry_after("120", datetime.now(timezone.utc)) == 120
