"""Client-side mirror of ANVISA's token-bucket rate limit.

The gateway used to report the bucket on every response (`X-RateLimit-Remaining`,
`X-RateLimit-Burst-Capacity`, `X-RateLimit-Replenish-Rate`; observed 25 and 1/s). Since about
2026-11-10 it sends none of them, while the limit itself still holds. We keep an estimate of
the bucket, count every response against it, take the headers as the truth when they come,
and sleep only when the bucket is about to run dry, so a tight loop never hits HTTP 429 and a
slow script never waits at all.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping


class Throttle:
    def __init__(
        self,
        burst: int = 25,
        rate: float = 1.0,
        reserve: int = 2,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.burst = burst
        self.rate = rate
        self.reserve = (
            reserve  # tokens we never spend, in case another process shares the client id
        )
        self._sleep = sleep
        self._clock = clock
        self.remaining: float = burst
        self._seen = clock()

    def estimate(self) -> float:
        """Tokens in the bucket right now, assuming it refilled since the last response."""
        return self._estimate_at(self._clock())

    def _estimate_at(self, now: float) -> float:
        return min(self.burst, self.remaining + (now - self._seen) * self.rate)

    def before(self) -> None:
        needed = self.reserve + 1
        short = needed - self.estimate()
        if short > 0:
            self._sleep(short / self.rate)

    def after(self, headers: Mapping[str, str], *, status: int | None = None) -> None:
        """Learn the bucket's state from a gateway response (the token request never comes
        here: `TokenAuth` sends it inside httpx's auth flow, to another host). A 429 means the
        bucket is empty, whatever the estimate said; a response without the headers spent one
        token, so it is taken off the estimate."""
        now = self._clock()
        remaining = _int(headers, "X-RateLimit-Remaining")
        if status == 429:
            self.remaining, self._seen = 0, now
            return
        if remaining is None:
            self.remaining, self._seen = max(0.0, self._estimate_at(now) - 1), now
            return
        self.remaining = remaining
        self.burst = _int(headers, "X-RateLimit-Burst-Capacity") or self.burst
        self.rate = _int(headers, "X-RateLimit-Replenish-Rate") or self.rate
        self._seen = now


def _int(headers: Mapping[str, str], name: str) -> int | None:
    value = headers.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
