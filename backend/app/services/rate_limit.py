"""
Tiny per-key in-memory rate limiter for the login endpoint.

Process-local — fine for the self-hosted single-instance deployment that this
app targets. If the app is ever fronted by multiple workers, move this to
Redis or an equivalent.

The intent is *defense against credential stuffing*, not full DoS protection.
bcrypt already makes individual attempts slow; this caps the burst rate so a
script can't grind through a wordlist.
"""
from __future__ import annotations

import time
from collections import deque
from threading import Lock


class RateLimiter:
    def __init__(self, max_attempts: int, window_seconds: float):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def check(self, key: str) -> bool:
        """Record an attempt. Return True if allowed, False if rate-limited."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.max_attempts:
                return False
            hits.append(now)
            return True

    def reset(self, key: str) -> None:
        """Clear history for a key — call on successful login."""
        with self._lock:
            self._hits.pop(key, None)


# Login: 10 attempts per 60s per IP. Generous enough for a fat-fingered user,
# tight enough that a script can't run a wordlist.
login_limiter = RateLimiter(max_attempts=10, window_seconds=60)
