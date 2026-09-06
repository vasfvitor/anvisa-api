"""Client-side mirror of ANVISA's token-bucket rate limit.

The gateway reports the bucket on every response (`X-RateLimit-Remaining`,
`X-RateLimit-Burst-Capacity`, `X-RateLimit-Replenish-Rate`; observed 25 and 1/s).
We keep an estimate of the bucket and sleep only when it is about to run dry, so a
tight loop never hits HTTP 429 and a slow script never waits at all.
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
        return min(self.burst, self.remaining + (self._clock() - self._seen) * self.rate)

    def before(self) -> None:
        needed = self.reserve + 1
        short = needed - self.estimate()
        if short > 0:
            self._sleep(short / self.rate)

    def after(self, headers: Mapping[str, str]) -> None:
        remaining = _int(headers, "X-RateLimit-Remaining")
        if remaining is None:
            return  # not from the gateway (e.g. the token endpoint); keep the estimate
        self.remaining = remaining
        self.burst = _int(headers, "X-RateLimit-Burst-Capacity") or self.burst
        self.rate = _int(headers, "X-RateLimit-Replenish-Rate") or self.rate
        self._seen = self._clock()


def _int(headers: Mapping[str, str], name: str) -> int | None:
    value = headers.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
