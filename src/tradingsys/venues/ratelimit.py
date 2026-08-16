"""A token bucket for staying inside a venue's published request limits.

Venues do not answer an overrun with a polite retry hint. Bybit blocks the IP for ten
minutes, which for a market data process means ten minutes of no data at all and a gap
that has to be backfilled. The cheap insurance is to run well under the ceiling and
never find out where it is exactly.

The bucket is deliberately simple: a steady refill rate, a burst capacity, and a wait
when there is nothing left. Time comes from an injected clock and waiting goes through
an injected sleep, so the tests measure the schedule rather than the wall clock and run
in microseconds. A limiter tested with real sleeps is a limiter tested at one rate,
once, on one machine.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, final

from tradingsys.core.errors import DomainError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["RateLimiter"]

type _Monotonic = Callable[[], float]
type _Sleep = Callable[[float], Awaitable[None]]


@final
class RateLimiter:
    """Allows ``rate`` operations per second, with a burst of ``capacity``.

    Fair across waiters: an :class:`asyncio.Lock` serialises acquisition, so callers
    are served in arrival order rather than whichever coroutine happens to wake first.
    """

    __slots__ = ("_capacity", "_lock", "_monotonic", "_rate", "_sleep", "_tokens", "_updated")

    def __init__(
        self,
        rate: float,
        *,
        capacity: float | None = None,
        monotonic: _Monotonic | None = None,
        sleep: _Sleep | None = None,
    ) -> None:
        """
        Args:
            rate: Sustained operations per second. Must be positive.
            capacity: Burst size. Defaults to one second's worth, which lets a short
                flurry through without ever exceeding the sustained rate.
            monotonic: Time source, in seconds. Injected for tests.
            sleep: Awaitable delay. Injected for tests.

        Raises:
            DomainError: ``rate`` or ``capacity`` is not positive.
        """
        if rate <= 0:
            raise DomainError(f"rate must be positive, got {rate}")
        bucket = rate if capacity is None else capacity
        if bucket <= 0:
            raise DomainError(f"capacity must be positive, got {capacity}")
        self._rate = rate
        self._capacity = bucket
        self._monotonic: _Monotonic = monotonic or time.monotonic
        self._sleep: _Sleep = sleep or asyncio.sleep
        self._tokens = bucket
        self._updated = self._monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def capacity(self) -> float:
        return self._capacity

    def tokens_available(self) -> float:
        """Tokens the bucket would hold right now, without consuming any."""
        return min(self._capacity, self._tokens + (self._monotonic() - self._updated) * self._rate)

    async def acquire(self, cost: float = 1.0) -> float:
        """Wait until ``cost`` tokens are available, then take them.

        Args:
            cost: Tokens this operation consumes. Endpoints that count for more than
                one request against the venue's budget pass their own weight.

        Returns:
            Seconds spent waiting, so a caller can log or meter the delay rather than
            discovering the throttle only as unexplained latency.

        Raises:
            DomainError: ``cost`` is not positive, or exceeds the bucket capacity and
                could therefore never be granted.
        """
        if cost <= 0:
            raise DomainError(f"cost must be positive, got {cost}")
        if cost > self._capacity:
            raise DomainError(
                f"cost {cost} exceeds the bucket capacity {self._capacity}, so this call "
                f"would wait forever; raise the capacity or split the work"
            )
        async with self._lock:
            now = self._monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            waited = 0.0
            if self._tokens < cost:
                # Reserve rather than re-check after sleeping. Re-checking looks more
                # careful and is a livelock: the refill is computed in binary floating
                # point, so a wait of exactly the missing tokens can land a fraction of
                # an ulp short, and the next wait is shorter still until the delay is
                # too small to change the clock at all. Booking the tokens against the
                # instant they exist makes progress arithmetic rather than hopeful, and
                # keeps the sustained rate exact in the process.
                waited = (cost - self._tokens) / self._rate
                self._tokens = cost
                self._updated = now + waited
                await self._sleep(waited)
            self._tokens -= cost
            return waited
