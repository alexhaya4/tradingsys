"""Time sources and identifier generation.

Every timestamp the system records is UTC and timezone aware. Naive datetimes are
rejected at the boundaries rather than being assumed to mean UTC, because that
assumption is wrong exactly once per deployment and is expensive when it is.

The :class:`Clock` protocol exists so that time dependent logic can be exercised
deterministically in tests without patching module globals.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import final

from tradingsys.core.errors import DomainError

__all__ = [
    "Clock",
    "FixedClock",
    "SystemClock",
    "ensure_utc",
    "new_id",
    "utc_now",
]


def utc_now() -> datetime:
    """The current instant, timezone aware, in UTC."""
    return datetime.now(tz=UTC)


def ensure_utc(instant: datetime, *, what: str = "timestamp") -> datetime:
    """Return ``instant`` converted to UTC, rejecting naive datetimes.

    Raises:
        DomainError: ``instant`` carries no timezone.
    """
    if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
        raise DomainError(
            f"{what} must be timezone aware; a naive datetime would be interpreted in "
            f"the host's local zone, which differs between developer machines and "
            f"production containers"
        )
    return instant.astimezone(UTC)


def new_id() -> str:
    """A fresh random identifier, used for correlation IDs and client order IDs.

    UUID4 hex without separators: 32 characters, safe in HTTP headers, log fields,
    and venue client order ID fields that reject punctuation.
    """
    return uuid.uuid4().hex


class Clock(ABC):
    """A source of the current time."""

    @abstractmethod
    def now(self) -> datetime:
        """Return the current instant, timezone aware, in UTC."""
        raise NotImplementedError


@final
class SystemClock(Clock):
    """The real wall clock."""

    __slots__ = ()

    def now(self) -> datetime:
        return utc_now()


@final
class FixedClock(Clock):
    """A clock that returns a controlled time, for deterministic tests and replay.

    This is production code rather than a test helper because backtesting and event
    replay need exactly the same behaviour: a clock whose value is supplied by the
    caller rather than by the host.
    """

    __slots__ = ("_now",)

    def __init__(self, now: datetime) -> None:
        self._now = ensure_utc(now, what="fixed clock time")

    def now(self) -> datetime:
        return self._now

    def set(self, instant: datetime) -> None:
        """Move the clock to ``instant``."""
        self._now = ensure_utc(instant, what="fixed clock time")

    def advance(self, seconds: float) -> datetime:
        """Move the clock forward and return the new time."""
        if seconds < 0:
            raise DomainError(f"cannot advance a clock backwards by {seconds} seconds")
        self._now = self._now + timedelta(seconds=seconds)
        return self._now
