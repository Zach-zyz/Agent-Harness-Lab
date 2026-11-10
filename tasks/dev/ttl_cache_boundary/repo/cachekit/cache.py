import time


class TTLCache:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._values = {}

    def set(self, key, value, ttl=None):
        expires_at = self.clock() + ttl if ttl else None
        self._values[key] = (value, expires_at)

    def get(self, key, default=None):
        if key not in self._values:
            return default
        value, expires_at = self._values[key]
        if expires_at and self.clock() > expires_at:
            del self._values[key]
            return default
        return value
