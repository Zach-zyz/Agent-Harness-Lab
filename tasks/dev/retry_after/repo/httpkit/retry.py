from email.utils import parsedate_to_datetime


def parse_retry_after(value, now):
    try:
        return int(value)
    except ValueError:
        retry_at = parsedate_to_datetime(value)
        return int((retry_at - now).total_seconds())
