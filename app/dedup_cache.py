"""Simple in-memory cache to avoid reprocessing the same commit of a PR.

⚠️ This cache is IN-MEMORY ONLY (a dict in the process) and is reset when the
server restarts. It is enough for a portfolio scope; in real production it would
be replaced by Redis or a database shared across instances.
"""

import time


class TTLCache:
    """Key cache with time-based expiration (TTL).

    Typical use: `seen(key)` does an atomic check-and-set (under the GIL, no
    internal await) — returns True if the key was ALREADY seen within the TTL;
    otherwise it records the key and returns False.
    """

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, float] = {}

    def _purge(self, now: float) -> None:
        expired = [k for k, t in self._store.items() if now - t > self._ttl]
        for k in expired:
            del self._store[k]

    def seen(self, key: str) -> bool:
        """Return True if the key was already seen (and still within the TTL).

        If it was not seen, record the current timestamp and return False.
        """
        now = time.monotonic()
        self._purge(now)
        if key in self._store:
            return True
        self._store[key] = now
        return False
