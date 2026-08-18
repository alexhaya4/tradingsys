"""Detecting holes in stored market data.

A gap is an absence of data during a period the venue was open. That qualification is
the whole design. Forex closes every weekend and on a list of holidays, and a detector
that treats any absence as a hole fires every Saturday, is ignored within a fortnight,
and is then silently useless on the morning a feed genuinely dies. A detector that
cries wolf is worse than no detector, because it also consumes the attention that would
have noticed.

So coverage is compared against :class:`~tradingsys.core.schedule.TradingSchedule`
rather than against the calendar. Weekends, holidays, and session boundaries are
absences by definition and are never reported.

Session boundaries move. They are defined in the venue's wall clock time, so the
corresponding UTC instants shift by an hour twice a year, and the schedule handles that
because it evaluates in a named IANA timezone. The tests cover both transitions
deliberately, since that is where this kind of logic actually breaks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, final

from tradingsys.core.clock import ensure_utc
from tradingsys.core.errors import DomainError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from tradingsys.core.schedule import Interval, TradingSchedule

__all__ = ["Gap", "coverage_from_timestamps", "find_gaps", "merge_coverage"]


@final
@dataclass(frozen=True, slots=True)
class Gap:
    """A period during which the venue was open and we hold no data.

    Attributes:
        start: First instant not covered, inclusive.
        end: First instant covered again, exclusive.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_utc(self.start, what="gap start"))
        object.__setattr__(self, "end", ensure_utc(self.end, what="gap end"))
        if self.end <= self.start:
            raise DomainError(
                f"a gap must cover a positive duration, got {self.start} to {self.end}"
            )

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def hours(self) -> tuple[datetime, ...]:
        """Every hour boundary this gap touches, for scheduling a backfill.

        Truncated to the hour and inclusive of the hour containing ``start``, because
        backfill is organised per hour and an hour that is partly missing has to be
        refetched whole.
        """
        first = self.start.replace(minute=0, second=0, microsecond=0)
        hours: list[datetime] = []
        cursor = first
        while cursor < self.end:
            hours.append(cursor)
            cursor += timedelta(hours=1)
        return tuple(hours)

    def __str__(self) -> str:
        return f"{self.start.isoformat()} to {self.end.isoformat()} ({self.duration})"


def coverage_from_timestamps(
    timestamps: Iterable[datetime], *, max_quiet: timedelta
) -> tuple[Interval, ...]:
    """Turn observed data points into the intervals they cover.

    Consecutive points closer together than ``max_quiet`` are treated as one continuous
    stretch of coverage. The parameter is what separates "the market was quiet" from
    "the feed stopped", and it has no universally correct value: a liquid pair trades
    several times a second, while a thin one can legitimately go a minute without a
    tick. It is therefore required rather than defaulted.

    Raises:
        DomainError: ``max_quiet`` is not positive, or a timestamp is naive.
    """
    if max_quiet <= timedelta(0):
        raise DomainError(f"max_quiet must be positive, got {max_quiet}")
    moments = sorted(ensure_utc(value, what="timestamp") for value in timestamps)
    if not moments:
        return ()

    intervals: list[Interval] = []
    start = moments[0]
    previous = moments[0]
    for moment in moments[1:]:
        if moment - previous > max_quiet:
            intervals.append((start, previous))
            start = moment
        previous = moment
    intervals.append((start, previous))
    # A single observation covers an instant, not a span. Widening it to max_quiet would
    # invent coverage we do not have, and dropping it would claim we hold nothing at an
    # instant we demonstrably observed. Zero width intervals are therefore kept.
    #
    # WHAT BREAKS IF THESE ARE FILTERED: an earlier version discarded them unless every
    # interval was zero width, so a lone tick between two busy stretches vanished while
    # a lone tick on its own survived. That is the reconnect case exactly: a stream that
    # delivers one quote and drops again reported no coverage at all, so the outage read
    # as longer than it was.
    return tuple(intervals)


def merge_coverage(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """Sort and combine overlapping or touching coverage intervals."""
    ordered = sorted(
        (ensure_utc(start, what="coverage start"), ensure_utc(end, what="coverage end"))
        for start, end in intervals
    )
    merged: list[Interval] = []
    for start, end in ordered:
        if end < start:
            raise DomainError(f"coverage interval ends before it starts: {start} to {end}")
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def find_gaps(
    schedule: TradingSchedule,
    coverage: Sequence[Interval],
    *,
    start: datetime,
    end: datetime,
    minimum: timedelta = timedelta(minutes=1),
) -> tuple[Gap, ...]:
    """Periods in ``[start, end)`` when the venue was open and we hold nothing.

    Args:
        schedule: The venue's trading hours. Absences outside these are not gaps.
        coverage: Intervals for which data is held. Need not be sorted or disjoint.
        start: Beginning of the window to examine.
        end: End of the window to examine, exclusive.
        minimum: Shortest absence worth reporting. Ticks are irregular by nature, so
            without a floor every quiet second during an open session becomes a gap.

    Returns:
        Gaps in ascending order.

    Raises:
        DomainError: Bounds are naive or reversed, or ``minimum`` is negative.
    """
    start = ensure_utc(start, what="window start")
    end = ensure_utc(end, what="window end")
    if end < start:
        raise DomainError(f"window end {end} is before start {start}")
    if minimum < timedelta(0):
        raise DomainError(f"minimum must not be negative, got {minimum}")

    held = merge_coverage(coverage)
    gaps: list[Gap] = []
    for open_at, close_at in schedule.open_intervals(start, end):
        cursor = open_at
        for covered_start, covered_end in held:
            if covered_end <= cursor:
                continue
            if covered_start >= close_at:
                break
            if covered_start > cursor:
                gaps.append(Gap(cursor, min(covered_start, close_at)))
            cursor = max(cursor, covered_end)
            if cursor >= close_at:
                break
        if cursor < close_at:
            gaps.append(Gap(cursor, close_at))

    return tuple(gap for gap in gaps if gap.duration >= minimum)
