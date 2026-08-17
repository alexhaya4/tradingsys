"""The caller for the backfill runner: work out what is missing, queue it, drain it.

The runner knows how to take an hour to a terminal state. It does not know which hours
it should be taking, and until something answered that it was a component with no
caller, which drifts from the system around it.

**Gaps come from the gap detector, not from a range.** Enqueueing every hour between two
dates would queue every weekend hour of every week and then complete them all with zero
rows, which works and wastes a day of fetching. The detector already compares recorded
coverage against the instrument's own trading schedule, so it names the hours that are
genuinely absent during hours the venue was open.

**Queueing is separate from draining.** They fail differently and are worth running
apart: queueing needs the database and the schedule, draining needs the feed. A run that
queues successfully and then cannot reach the feed has still made progress, and the
queue is where that progress lives.

**Nothing here decides how far back to go.** The window comes from configuration, and
the job refuses a window it was not given rather than choosing a default, because
"how much history" is a research decision with a storage cost and not a thing for a
scheduler to assume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, final

from tradingsys.core.errors import TradingSysError
from tradingsys.marketdata.backfill import BackfillRunner, hours_between
from tradingsys.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tradingsys.core.instrument import Instrument
    from tradingsys.core.schedule import TradingSchedule
    from tradingsys.marketdata.backfill import BackfillPlan, BackfillStats, HourFetcher, TickStore
    from tradingsys.persistence.backfill import BackfillRepository

__all__ = ["BackfillJob", "JobReport", "missing_open_hours"]

logger = get_logger("marketdata.backfill_job")


@final
@dataclass(frozen=True, slots=True)
class JobReport:
    """What one job run queued and drained.

    Attributes:
        queued: Hours added to the queue this run. Zero once the queue holds the
            window already, which is the steady state rather than a problem.
        open_hours: Hours in the window the venue was open for.
        stats: What the runner did with whatever it claimed.
    """

    queued: int
    open_hours: int
    stats: BackfillStats

    def summary(self) -> str:
        return (
            f"queued {self.queued} of {self.open_hours} open hours, "
            f"completed {self.stats.completed}, failed {self.stats.failed}, "
            f"rows {self.stats.rows_written}, empty {self.stats.empty_hours}"
        )


def missing_open_hours(
    window_start: datetime,
    window_end: datetime,
    *,
    schedule: TradingSchedule,
    already_covered: frozenset[datetime],
) -> tuple[datetime, ...]:
    """Hours in the window during which the venue was open and nothing is recorded.

    A weekend hour is not a gap and never becomes one, so queueing it would mean
    fetching a file that does not exist in order to record that it does not exist. The
    schedule is the instrument's own, from venue metadata, so a broker that changes its
    session hours changes this on the next registry sync rather than on a code change.

    Args:
        window_start: Inclusive, aligned to the hour, timezone aware.
        window_end: Exclusive, same.
        schedule: The instrument's trading schedule.
        already_covered: Hour starts that are already recorded or already queued.
    """
    open_intervals = schedule.open_intervals(window_start, window_end)
    missing: list[datetime] = []
    for hour in hours_between(window_start, window_end):
        if hour in already_covered:
            continue
        # An hour counts as open if the venue was open at any point in it. A partly
        # open hour still has ticks in it, and skipping it would leave a real hole.
        hour_end = hour + timedelta(hours=1)
        if any(start < hour_end and hour < end for start, end in open_intervals):
            missing.append(hour)
    return tuple(missing)


@final
class BackfillJob:
    """Queues the missing hours for one instrument, then drains the queue."""

    __slots__ = ("_fetcher", "_instrument", "_plan", "_queue", "_store")

    def __init__(
        self,
        plan: BackfillPlan,
        instrument: Instrument,
        *,
        queue: BackfillRepository,
        fetcher: HourFetcher,
        store: TickStore,
    ) -> None:
        if instrument.id != plan.instrument_id:
            raise TradingSysError(
                f"the plan is for {plan.instrument_id} and the instrument is "
                f"{instrument.id}; a backfill written against the wrong schedule would "
                f"queue the wrong hours"
            )
        self._plan = plan
        self._instrument = instrument
        self._queue = queue
        self._fetcher = fetcher
        self._store = store

    async def run(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        already_covered: frozenset[datetime] = frozenset(),
        max_hours: int | None = None,
    ) -> JobReport:
        """Queue what is missing in the window, then drain what is queued.

        Args:
            window_start: Inclusive, aligned, timezone aware.
            window_end: Exclusive, same.
            already_covered: Hours known to be recorded already. Passing none is safe
                and merely queues hours the queue will find already present, because
                enqueueing is idempotent.
            max_hours: Bound on how much to drain in this run.

        Raises:
            TradingSysError: The window is malformed. It is not defaulted, because how
                far back to backfill is a decision with a storage cost.
        """
        missing = missing_open_hours(
            window_start,
            window_end,
            schedule=self._instrument.schedule,
            already_covered=already_covered,
        )
        queued = await self._queue.enqueue(self._plan.source, self._plan.instrument_row_id, missing)

        runner = BackfillRunner(
            self._plan, queue=self._queue, fetcher=self._fetcher, store=self._store
        )
        stats = await runner.run(max_hours=max_hours)

        report = JobReport(queued=queued, open_hours=len(missing), stats=stats)
        logger.info(
            "backfill job finished",
            source=self._plan.source,
            instrument=str(self._instrument.id),
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            queued=report.queued,
            open_hours=report.open_hours,
            completed=stats.completed,
            failed=stats.failed,
            rows=stats.rows_written,
        )
        return report


def recorded_hours(timestamps: Sequence[datetime]) -> frozenset[datetime]:
    """Hour starts covered by a set of recorded timestamps.

    A convenience for callers holding tick timestamps rather than hour buckets. Any
    tick inside an hour marks that hour covered, which is the same rule the gap
    detector applies.
    """
    return frozenset(stamp.replace(minute=0, second=0, microsecond=0) for stamp in timestamps)
