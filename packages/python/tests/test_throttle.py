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


def test_headers_from_gateway_update_limits_and_others_are_ignored():
    throttle, _ = make()
    throttle.after(gateway_headers(5, burst="50", rate="2"))
    assert (throttle.remaining, throttle.burst, throttle.rate) == (5, 50, 2)
    throttle.after({"Content-Type": "application/json"})  # e.g. the token endpoint
    assert throttle.remaining == 5
