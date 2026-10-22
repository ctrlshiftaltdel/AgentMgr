"""In-process per-principal/per-operation rate limiting.

The limiter is deliberately process-local: Issue #80 explicitly permits an
in-process implementation and forbids adding another dependency for it.  A
multi-process deployment must front this with a shared limiter; that limitation
is recorded in the client integration docs.

The key is ``(principal_id, operation)`` and the operation string is the same
for the MCP tool and its REST mirror, so both transports share one quota.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from app.api.errors import ApiError

__all__ = ["GOVERNANCE_LIMITER", "RateLimiter"]


class RateLimiter:
    """A fixed-window counter keyed by an opaque string."""

    def __init__(
        self,
        *,
        limit: int = 120,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.limit = int(limit)
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Record one hit, raising a stable 429 when the window is exhausted."""
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                raise ApiError(429, "rate_limited", "rate limit exceeded")
            hits.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


#: Shared by the REST router and the MCP tools.  Tests may replace the object
#: (or call ``reset``) to make the boundary deterministic.
GOVERNANCE_LIMITER = RateLimiter()
