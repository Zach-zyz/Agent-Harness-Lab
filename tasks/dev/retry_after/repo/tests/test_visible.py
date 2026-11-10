from datetime import datetime, timezone

import pytest

from httpkit import parse_retry_after


def test_http_date_rounds_up_and_past_clamps_to_zero() -> None:
    now = datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=timezone.utc)
    assert parse_retry_after("Thu, 01 Jan 2026 00:00:02 GMT", now) == 2
    assert parse_retry_after("Wed, 31 Dec 2025 23:59:00 GMT", now) == 0


@pytest.mark.parametrize("value", ["-1", "+2", "1.5", "later", None])
def test_invalid_values_raise(value) -> None:
    with pytest.raises(ValueError):
        parse_retry_after(value, datetime.now(timezone.utc))
