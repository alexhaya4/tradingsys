"""Carry cost conventions.

Holding a leveraged position costs money overnight, but the two venue families charge
it in incompatible ways.

Forex brokers apply a *swap* once per day at a fixed local rollover time, quoted in
price points per unit held, and charge three nights on one weekday of the week to cover
the weekend value dates. Crypto perpetual venues apply a *funding rate* on a fixed
interval (typically every eight hours, anchored to UTC midnight), quoted as a fraction
of notional.

This module models when those charges occur. It does not model how large they are: the
rates themselves are venue data, fetched through
:class:`~tradingsys.venues.base.MarketDataSource`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Self, final
from zoneinfo import ZoneInfo

from tradingsys.core.errors import DomainError
from tradingsys.core.schedule import Weekday

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tradingsys.core.schedule import TradingSchedule

__all__ = [
    "FinancingModel",
    "FinancingSpec",
    "FundingEvent",
    "RolloverEvent",
]

_MAX_EVENT_DAYS = 3660
"""Guard on event expansion: ten years of daily rollovers is far past any sane query."""


class FinancingModel(StrEnum):
    """How a venue charges for holding a position overnight."""

    NONE = "none"
    """No carry cost. Unleveraged crypto spot, where you hold the asset outright."""

    SWAP_POINTS = "swap_points"
    """Forex style: a per unit charge in price points, applied at a daily rollover."""

    FUNDING_RATE = "funding_rate"
    """Perpetual swap style: a periodic rate applied to position notional."""

    BORROW_RATE = "borrow_rate"
    """Margin borrow interest, accrued per interval on the borrowed leg."""


@final
@dataclass(frozen=True, slots=True)
class FundingEvent:
    """A scheduled funding settlement for a perpetual style instrument."""

    at: datetime
    """Instant of settlement, timezone aware."""


@final
@dataclass(frozen=True, slots=True)
class RolloverEvent:
    """A scheduled daily rollover for a swap financed instrument."""

    at: datetime
    """Instant of the rollover, timezone aware, in the schedule's timezone."""

    nights: int
    """Number of value date nights charged, normally 1 and 3 on the triple day."""


@final
@dataclass(frozen=True, slots=True)
class FinancingSpec:
    """The carry convention for one instrument.

    Attributes:
        model: Which charging mechanism applies.
        funding_interval: Period between funding settlements. Required for
            :attr:`FinancingModel.FUNDING_RATE` and :attr:`FinancingModel.BORROW_RATE`.
        funding_anchor: A timezone aware instant at which a funding settlement is
            known to occur. All other settlements are derived as anchor plus a whole
            number of intervals. Required alongside ``funding_interval``.
        rollover_time: Local wall clock time of the daily rollover. Required for
            :attr:`FinancingModel.SWAP_POINTS`.
        triple_rollover_day: Weekday on which the venue charges three nights instead
            of one, covering the weekend value dates. Optional: some venues spread the
            weekend charge differently or not at all.
    """

    model: FinancingModel
    funding_interval: timedelta | None = None
    funding_anchor: datetime | None = None
    rollover_time: time | None = None
    triple_rollover_day: Weekday | None = None

    def __post_init__(self) -> None:
        periodic = self.model in (FinancingModel.FUNDING_RATE, FinancingModel.BORROW_RATE)
        if periodic:
            if self.funding_interval is None or self.funding_anchor is None:
                raise DomainError(
                    f"financing model {self.model} requires both funding_interval and "
                    f"funding_anchor so settlement times can be derived"
                )
            if self.funding_interval <= timedelta(0):
                raise DomainError(f"funding_interval must be positive, got {self.funding_interval}")
            if timedelta(days=1) % self.funding_interval != timedelta(0):
                raise DomainError(
                    f"funding_interval {self.funding_interval} must divide 24 hours evenly, "
                    f"otherwise settlement times drift across the day"
                )
            if self.funding_anchor.tzinfo is None:
                raise DomainError("funding_anchor must be timezone aware")
        elif self.funding_interval is not None or self.funding_anchor is not None:
            raise DomainError(
                f"financing model {self.model} does not use funding_interval or funding_anchor"
            )

        if self.model is FinancingModel.SWAP_POINTS:
            if self.rollover_time is None:
                raise DomainError("financing model swap_points requires a rollover_time")
            if self.rollover_time.tzinfo is not None:
                raise DomainError(
                    "rollover_time must be a naive wall clock time; the timezone comes "
                    "from the instrument's trading schedule"
                )
        elif self.rollover_time is not None or self.triple_rollover_day is not None:
            raise DomainError(
                f"financing model {self.model} does not use rollover_time or triple_rollover_day"
            )

    # ------------------------------------------------------------------
    # constructors
    # ------------------------------------------------------------------

    @classmethod
    def none(cls) -> Self:
        """No overnight cost."""
        return cls(model=FinancingModel.NONE)

    @classmethod
    def swap(
        cls,
        rollover_time: time,
        triple_rollover_day: Weekday | None = Weekday.WEDNESDAY,
    ) -> Self:
        """Daily swap at a local rollover time.

        The default triple day is Wednesday, which is the convention for spot forex
        under T+2 settlement. Venues that differ pass their own value or ``None``.
        """
        return cls(
            model=FinancingModel.SWAP_POINTS,
            rollover_time=rollover_time,
            triple_rollover_day=triple_rollover_day,
        )

    @classmethod
    def funding(cls, interval: timedelta, anchor: datetime) -> Self:
        """Periodic funding on a perpetual style instrument."""
        return cls(
            model=FinancingModel.FUNDING_RATE, funding_interval=interval, funding_anchor=anchor
        )

    # ------------------------------------------------------------------
    # event expansion
    # ------------------------------------------------------------------

    def funding_events_between(self, start: datetime, end: datetime) -> tuple[FundingEvent, ...]:
        """Funding settlements in the half open interval ``(start, end]``.

        ``start`` is exclusive so that repeatedly calling with the previous end as the
        new start neither repeats nor skips a settlement.
        """
        if self.funding_interval is None or self.funding_anchor is None:
            raise DomainError(
                f"financing model {self.model} has no funding schedule; check the model "
                f"before asking for funding events"
            )
        _require_ordered_aware(start, end)
        interval = int(self.funding_interval.total_seconds())
        anchor = int(self.funding_anchor.timestamp())
        first = int(start.timestamp())
        last = int(end.timestamp())
        # Smallest k with anchor + k*interval > start.
        steps = (first - anchor) // interval + 1
        events: list[FundingEvent] = []
        moment = anchor + steps * interval
        zone = self.funding_anchor.tzinfo
        while moment <= last:
            events.append(FundingEvent(at=datetime.fromtimestamp(moment, tz=zone)))
            if len(events) > _MAX_EVENT_DAYS * 24:
                raise DomainError(
                    f"funding expansion from {start} to {end} exceeded the safety limit; "
                    f"narrow the range"
                )
            moment += interval
        return tuple(events)

    def rollover_events_between(
        self, start: datetime, end: datetime, schedule: TradingSchedule
    ) -> tuple[RolloverEvent, ...]:
        """Daily rollovers in the half open interval ``(start, end]``.

        A rollover only occurs on a day the venue is actually trading at the rollover
        time, so weekends and holidays are skipped rather than accumulating a charge.
        The triple day carries three nights; every other trading day carries one.
        """
        if self.rollover_time is None:
            raise DomainError(
                f"financing model {self.model} has no daily rollover; check the model "
                f"before asking for rollover events"
            )
        _require_ordered_aware(start, end)
        zone = ZoneInfo(schedule.timezone)
        first_day = start.astimezone(zone).date()
        last_day = end.astimezone(zone).date()
        span = (last_day - first_day).days
        if span > _MAX_EVENT_DAYS:
            raise DomainError(
                f"rollover expansion from {start} to {end} spans {span} days, beyond the "
                f"safety limit of {_MAX_EVENT_DAYS}; narrow the range"
            )
        events: list[RolloverEvent] = []
        for offset in range(span + 1):
            day = first_day + timedelta(days=offset)
            moment = datetime.combine(day, self.rollover_time, tzinfo=zone)
            if not (start < moment <= end):
                continue
            if not schedule.is_open(moment):
                continue
            nights = 3 if self.triple_rollover_day == day.weekday() else 1
            events.append(RolloverEvent(at=moment, nights=nights))
        return tuple(events)

    # ------------------------------------------------------------------
    # serialisation
    # ------------------------------------------------------------------

    def to_mapping(self) -> dict[str, Any]:
        """Serialise for storage in a JSONB column."""
        return {
            "model": self.model.value,
            "funding_interval_seconds": (
                int(self.funding_interval.total_seconds())
                if self.funding_interval is not None
                else None
            ),
            "funding_anchor": (
                self.funding_anchor.isoformat() if self.funding_anchor is not None else None
            ),
            "rollover_time": (
                self.rollover_time.isoformat() if self.rollover_time is not None else None
            ),
            "triple_rollover_day": (
                self.triple_rollover_day.name if self.triple_rollover_day is not None else None
            ),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild a spec from :meth:`to_mapping` output."""
        if "model" not in data:
            raise DomainError("financing mapping is missing 'model'")
        try:
            model = FinancingModel(str(data["model"]))
        except ValueError:
            raise DomainError(f"{data['model']!r} is not a known financing model") from None
        interval_seconds = data.get("funding_interval_seconds")
        anchor_raw = data.get("funding_anchor")
        rollover_raw = data.get("rollover_time")
        triple_raw = data.get("triple_rollover_day")
        return cls(
            model=model,
            funding_interval=(
                timedelta(seconds=int(interval_seconds)) if interval_seconds is not None else None
            ),
            funding_anchor=(
                datetime.fromisoformat(str(anchor_raw)) if anchor_raw is not None else None
            ),
            rollover_time=(
                time.fromisoformat(str(rollover_raw)) if rollover_raw is not None else None
            ),
            triple_rollover_day=(
                Weekday[str(triple_raw).upper()] if triple_raw is not None else None
            ),
        )


def _require_ordered_aware(start: datetime, end: datetime) -> None:
    for label, value in (("start", start), ("end", end)):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise DomainError(f"{label} must be timezone aware")
    if end < start:
        raise DomainError(f"end {end} is before start {start}")
