from anvisa.throttle import Throttle


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def make():
    clock = Clock()
    return Throttle(sleep=clock.sleep, clock=clock), clock


def gateway_headers(remaining, burst="25", rate="1"):
    return {
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Burst-Capacity": burst,
        "X-RateLimit-Replenish-Rate": rate,
    }


def test_full_bucket_never_sleeps():
    throttle, clock = make()
    for remaining in range(24, 2, -1):
        throttle.before()
        throttle.after(gateway_headers(remaining))
    assert clock.slept == []


def test_sleeps_only_when_reserve_would_be_spent():
    throttle, clock = make()
    throttle.after({"X-RateLimit-Remaining": "1"})
    throttle.before()  # need reserve+1 = 3 tokens, have 1 -> wait 2 s at 1 token/s
    assert clock.slept == [2.0]
    throttle.before()  # the wait refilled the bucket
    assert clock.slept == [2.0]


def test_refill_is_estimated_from_elapsed_time():
    throttle, clock = make()
    throttle.after({"X-RateLimit-Remaining": "0"})
    clock.now += 10
    assert throttle.estimate() == 10
    throttle.before()
    assert clock.slept == []


def test_headers_from_gateway_update_limits_and_a_response_without_them_counts_one():
    throttle, _ = make()
    throttle.after(gateway_headers(5, burst="50", rate="2"))
    assert (throttle.remaining, throttle.burst, throttle.rate) == (5, 50, 2)
    throttle.after({"Content-Type": "application/json"})
    assert (throttle.remaining, throttle.burst, throttle.rate) == (4, 50, 2)


def test_without_headers_a_tight_loop_never_overdraws_the_gateway_bucket():
    # Since about 2026-11-10 the gateway sends no X-RateLimit-* header. The old throttle kept
    # its estimate at 25 and never slept: anvisa-feeds got 429 on ~234 of 318 subfilas a day.
    throttle, clock = make()
    tokens, last = 25.0, 0.0  # the gateway's own bucket: burst 25, refill 1/s
    for _ in range(318):
        throttle.before()
        tokens, last = min(25.0, tokens + (clock.now - last)), clock.now
        tokens -= 1
        assert tokens >= 0, f"429 at t={clock.now}"
        throttle.after({})
    assert 290 <= clock.now <= 300  # ~1 request/s once the burst is spent


def test_a_429_empties_the_estimate():
    throttle, clock = make()
    throttle.after(gateway_headers(20), status=429)
    assert throttle.estimate() == 0
    throttle.before()  # reserve+1 = 3 tokens at 1/s
    assert clock.slept == [3.0]
