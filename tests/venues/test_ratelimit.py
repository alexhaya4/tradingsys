"""Tests for the request token bucket.

Time is injected, so these assert the schedule the limiter produces rather than how
long the suite happened to sleep for. A limiter tested against the wall clock is
tested once, at one rate, on one machine, and is flaky everywhere else.
"""

from __future__ import annotations

import asyncio

import pytest

from tradingsys.core.errors import DomainError
from tradingsys.venues.ratelimit import RateLimiter


class FakeTime:
    """A monotonic clock that only advances when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeTime:
    return FakeTime()


def limiter(clock: FakeTime, rate: float, capacity: float | None = None) -> RateLimiter:
    return RateLimiter(rate, capacity=capacity, monotonic=clock.monotonic, sleep=clock.sleep)


class TestBurstThenSteadyRate:
    async def test_the_first_calls_up_to_capacity_do_not_wait(self, clock: FakeTime) -> None:
        bucket = limiter(clock, rate=10, capacity=3)
        assert [await bucket.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
        assert clock.sleeps == []

    async def test_the_next_call_waits_exactly_one_interval(self, clock: FakeTime) -> None:
        bucket = limiter(clock, rate=10, capacity=3)
        for _ in range(3):
            await bucket.acquire()
        assert await bucket.acquire() == pytest.approx(0.1)

    async def test_a_sustained_run_settles_at_the_configured_rate(self, clock: FakeTime) -> None:
        # Twenty calls at five per second, after a burst of one: nineteen intervals.
        bucket = limiter(clock, rate=5, capacity=1)
        for _ in range(20):
            await bucket.acquire()
        assert clock.now == pytest.approx(19 * 0.2)

    async def test_tokens_refill_while_idle(self, clock: FakeTime) -> None:
        bucket = limiter(clock, rate=10, capacity=5)
        for _ in range(5):
            await bucket.acquire()
        clock.now += 1.0
        assert [await bucket.acquire() for _ in range(5)] == [0.0] * 5

    async def test_the_bucket_does_not_fill_past_capacity(self, clock: FakeTime) -> None:
        # An hour of idleness does not buy an hour's worth of burst, which is the
        # difference between a limiter and a counter.
        bucket = limiter(clock, rate=10, capacity=2)
        clock.now += 3600
        assert bucket.tokens_available() == pytest.approx(2)
        await bucket.acquire()
        await bucket.acquire()
        assert await bucket.acquire() == pytest.approx(0.1)

    async def test_capacity_defaults_to_one_second_of_rate(self, clock: FakeTime) -> None:
        bucket = limiter(clock, rate=4)
        assert bucket.capacity == 4
        assert bucket.rate == 4


class TestWeightedCalls:
    async def test_a_costly_call_consumes_proportionally(self, clock: FakeTime) -> None:
        bucket = limiter(clock, rate=10, capacity=10)
        await bucket.acquire(cost=8)
        assert bucket.tokens_available() == pytest.approx(2)

    async def test_a_cost_beyond_capacity_is_refused_rather_than_waited_on(
        self, clock: FakeTime
    ) -> None:
        # Waiting would never end, and a call that never ends is worse than a failure.
        bucket = limiter(clock, rate=10, capacity=5)
        with pytest.raises(DomainError, match="exceeds the bucket capacity"):
            await bucket.acquire(cost=6)

    @pytest.mark.parametrize("cost", [0, -1])
    async def test_a_non_positive_cost_is_refused(self, clock: FakeTime, cost: float) -> None:
        with pytest.raises(DomainError, match="cost must be positive"):
            await limiter(clock, rate=10).acquire(cost=cost)


class TestFairness:
    async def test_waiters_are_served_in_arrival_order(self, clock: FakeTime) -> None:
        # Without the lock, whichever coroutine the loop happens to wake first takes
        # the token, and one unlucky request can be starved indefinitely.
        bucket = limiter(clock, rate=1, capacity=1)
        order: list[int] = []

        async def call(index: int) -> None:
            await bucket.acquire()
            order.append(index)

        await asyncio.gather(*(call(index) for index in range(4)))
        assert order == [0, 1, 2, 3]


class TestConstruction:
    @pytest.mark.parametrize("rate", [0, -1.5])
    def test_a_non_positive_rate_is_refused(self, rate: float) -> None:
        with pytest.raises(DomainError, match="rate must be positive"):
            RateLimiter(rate)

    @pytest.mark.parametrize("capacity", [0, -1])
    def test_a_non_positive_capacity_is_refused(self, capacity: float) -> None:
        with pytest.raises(DomainError, match="capacity must be positive"):
            RateLimiter(10, capacity=capacity)

    async def test_the_default_clock_is_the_real_one(self) -> None:
        # The injected clock is a test affordance, not the production path, so the
        # production path gets exercised at least once.
        bucket = RateLimiter(1000)
        assert await bucket.acquire() == 0.0


class TestProgressUnderFloatingPointRefill:
    """The bug this class exists for hung the suite rather than failing it.

    A limiter that sleeps for the missing tokens and then re-measures the clock can
    come back a fraction of an ulp short, because the refill is computed in binary
    floating point and the interval is usually not representable. The next wait is
    then shorter, and the one after shorter still, until the delay no longer changes
    the clock and the loop spins forever. Reserving the tokens against the instant they
    will exist makes each pass terminate by construction.
    """

    @pytest.mark.parametrize("rate", [3.0, 5.0, 7.0, 9.0, 11.0])
    async def test_many_sequential_calls_terminate_at_awkward_rates(
        self, clock: FakeTime, rate: float
    ) -> None:
        # None of these intervals is exactly representable in binary.
        bucket = limiter(clock, rate=rate, capacity=1)
        for _ in range(200):
            await bucket.acquire()
        assert clock.now == pytest.approx(199 / rate)

    async def test_a_long_idle_then_a_run_terminates(self, clock: FakeTime) -> None:
        bucket = limiter(clock, rate=10, capacity=2)
        clock.now += 3600
        for _ in range(50):
            await bucket.acquire()
        assert clock.now == pytest.approx(3600 + 48 * 0.1)

    async def test_the_sustained_rate_does_not_drift_over_a_long_run(self, clock: FakeTime) -> None:
        # Reserving tokens against a future instant could accumulate error in the other
        # direction, letting the bucket run slightly fast. Over a thousand calls at
        # seven per second, it does not.
        bucket = limiter(clock, rate=7, capacity=1)
        for _ in range(1000):
            await bucket.acquire()
        assert clock.now == pytest.approx(999 / 7, rel=1e-12)
