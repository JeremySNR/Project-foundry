"""In-process rate limiting for the webhook and API surfaces.

The webhook endpoints (signed intake, approvals) and the API surfaces (approval
POST, run reads, metrics) are the only ways into Foundry, and they are exposed
to the network. Signature/token checks stop *unauthorised* callers, but they do
nothing to stop a flood of *authorised-looking* requests - a captured webhook
replayed in a tight loop, a misbehaving integration, or a brute-force probe of
the bearer token. This module adds a coarse per-client request cap so a single
source cannot exhaust the process.

Design, deliberately small:

- A fixed-window counter per ``(bucket, client)`` key. Fixed windows are simple,
  deterministic, and trivially testable with an injected clock; the small
  boundary burst they allow is irrelevant against the abuse this guards.
- Two buckets - ``webhook`` and ``api`` - so a flood on one surface cannot
  starve the budget of the other.
- Pure, clock-injectable core (:class:`RateLimiter`) with no FastAPI imports, so
  the algorithm is unit-tested without a server.

Scope and limits (documented, not hidden): the counters live in process memory,
so the cap is **per process**, not shared across workers - the same constraint
as the in-memory webhook dedup set, and the same future fix (a shared store).
It is a first line of defence, not a distributed quota. Clients are keyed by
the direct peer address; behind a proxy that is the proxy's address unless the
deployment terminates correctly, so document the proxy caveat rather than trust
a spoofable forwarded header by default.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

# Sweep stale window entries once the table grows past this, so a long-lived
# process facing many distinct client keys does not leak memory unbounded.
_SWEEP_THRESHOLD = 4096


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of a single :meth:`RateLimiter.check` call."""

    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # whole seconds until the current window resets


class RateLimiter:
    """Fixed-window request counter, keyed by an opaque string.

    ``limit`` requests are allowed per ``window_seconds``; the ``limit + 1``-th
    within the same window is refused. ``clock`` is injectable so tests advance
    time without sleeping; it defaults to a monotonic clock so wall-clock jumps
    (NTP, suspend/resume) cannot widen or collapse a window.
    """

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0, got {window_seconds}")
        self._limit = limit
        self._window = float(window_seconds)
        self._clock = clock
        # key -> (window_start, count). Guarded by a lock because Starlette can
        # dispatch requests concurrently across threads.
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def check(self, key: str) -> RateLimitResult:
        """Account for one request against ``key`` and report whether to allow it.

        Allowed requests are counted; a refused request is *not* counted, so a
        client hammering a limit that has already tripped cannot keep pushing
        the window forward and starve itself indefinitely past the reset.
        """
        now = self._clock()
        with self._lock:
            if len(self._buckets) > _SWEEP_THRESHOLD:
                self._sweep(now)
            window_start, count = self._buckets.get(key, (now, 0))
            if now - window_start >= self._window:
                # Window elapsed: start a fresh one.
                window_start, count = now, 0
            retry_after = max(0, int(window_start + self._window - now + 0.999))
            if count >= self._limit:
                return RateLimitResult(
                    allowed=False,
                    limit=self._limit,
                    remaining=0,
                    retry_after=max(1, retry_after),
                )
            count += 1
            self._buckets[key] = (window_start, count)
            return RateLimitResult(
                allowed=True,
                limit=self._limit,
                remaining=self._limit - count,
                retry_after=retry_after,
            )

    def _sweep(self, now: float) -> None:
        """Drop entries whose window has fully elapsed. Caller holds the lock."""
        expired = [
            key
            for key, (start, _count) in self._buckets.items()
            if now - start >= self._window
        ]
        for key in expired:
            del self._buckets[key]
