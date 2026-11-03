"""Simple in-memory per-user rate limiter."""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_per_minute: int = 20):
        self.max_per_minute = max_per_minute
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        q = self._hits[user_id]
        while q and now - q[0] > 60.0:
            q.popleft()
        if len(q) >= self.max_per_minute:
            return False
        q.append(now)
        return True
