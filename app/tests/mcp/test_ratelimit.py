"""Per-principal/per-operation in-process rate limiting (no database)."""

from __future__ import annotations

import pytest

from app.api.errors import ApiError
from app.api.rest.ratelimit import RateLimiter


def test_limit_is_per_key_and_recovers_after_the_window():
    now = [1000.0]
    limiter = RateLimiter(limit=2, window_seconds=60, clock=lambda: now[0])

    limiter.check("prin_0001:governance.get_task")
    limiter.check("prin_0001:governance.get_task")
    with pytest.raises(ApiError) as exc:
        limiter.check("prin_0001:governance.get_task")
    assert exc.value.code == "rate_limited"
    assert exc.value.status_code == 429

    # Another principal or operation has its own bucket.
    limiter.check("prin_0001:governance.submit_task")
    limiter.check("prin_0002:governance.get_task")

    now[0] += 61
    limiter.check("prin_0001:governance.get_task")


def test_reset_clears_every_bucket():
    limiter = RateLimiter(limit=1, window_seconds=60)
    limiter.check("key")
    with pytest.raises(ApiError):
        limiter.check("key")
    limiter.reset()
    limiter.check("key")
