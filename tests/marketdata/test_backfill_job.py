"""The backfill caller: which hours it decides are missing, and what it does with them.

The runner's job is to take an hour to a terminal state and is tested elsewhere. This
is about the decision that precedes it, where a wrong answer is expensive in one
direction and merely wasteful in the other.

Queueing a closed hour costs a fetch that returns nothing. Failing to queue an open one
leaves a hole that nothing will ever look for again, because the queue is the only
record of what was meant to be fetched. So the tests lean on the second.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

import pytest

from tests.factories import eurusd
from tradingsys.core.errors import TradingSysError
from tradingsys.core.instrument import InstrumentId
from tradingsys.core.schedule import TradingSchedule, Weekday, WeeklySession
from tradingsys.marketdata.backfill import BackfillPlan
from tradingsys.marketdata.backfill_job import BackfillJob, missing_open_hours, recorded_hours

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tradingsys.core.provenance import TickSource
    from tradingsys.persistence.backfill import BackfillHour
    from tradingsys.venues.models import Quote

pytestmark = pytest.mark.asyncio

# 2026-08-10 is a Monday.
MONDAY = datetime(2026, 8, 10, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 8, 15, 0, tzinfo=UTC)

WEEKDAYS_ONLY = TradingSchedule(
    timezone="UTC",
    sessions=(
        WeeklySession(
            open_day=Weekday.MONDAY,
            open_time=time(0, 0),
            close_day=Weekday.FRIDAY,
            close_time=time(23, 0),
        ),
    ),
)


@dataclass(slots=True)
class RecordingQueue:
    enqueued: list[datetime] = field(default_factory=list)

    async def enqueue(self, source: str, row_id: int, hours: Sequence[datetime]) -> int:
        del source, row_id
        self.enqueued.extend(hours)
        return len(list(hours))

    async def claim(
        self, source: str, *, limit: int, stale_after: timedelta
    ) -> Sequence[BackfillHour]:
        del source, limit, stale_after
        return []

    async def mark_complete(self, hour: BackfillHour, *, rows_written: int) -> None:
        del hour, rows_written
        raise AssertionError("nothing should be claimed in these tests")

    async def mark_failed(self, hour: BackfillHour, *, error: str) -> None:
        del hour, error
        raise AssertionError("nothing should be claimed in these tests")


@dataclass(slots=True)
class IdleFeed:
    async def fetch(self, url: str) -> bytes | None:
        del url
        return None


@dataclass(slots=True)
class IdleStore:
    async def store_ticks(
        self, instrument_row_id: int, source: TickSource, ticks: Sequence[Quote]
    ) -> int:
        del instrument_row_id, source
        return len(ticks)


def plan_for(instrument_id: InstrumentId) -> BackfillPlan:
    return BackfillPlan(
        source="dukascopy",
        instrument_id=instrument_id,
        instrument_row_id=1,
        venue_symbol="EURUSD",
        digits=5,
        concurrency=3,
        max_attempts_per_hour=2,
        backoff_seconds=0.001,
        stale_claim_after=timedelta(minutes=30),
    )


class TestWhichHoursAreMissing:
    async def test_a_closed_weekend_hour_is_never_queued(self) -> None:
        # Queueing it means fetching a file that does not exist in order to record that
        # it does not exist, for every weekend hour of every week of history.
        missing = missing_open_hours(
            SATURDAY,
            SATURDAY + timedelta(hours=24),
            schedule=WEEKDAYS_ONLY,
            already_covered=frozenset(),
        )
        assert missing == ()

    async def test_open_hours_are_queued(self) -> None:
        missing = missing_open_hours(
            MONDAY,
            MONDAY + timedelta(hours=5),
            schedule=WEEKDAYS_ONLY,
            already_covered=frozenset(),
        )
        assert missing == tuple(MONDAY + timedelta(hours=index) for index in range(5))

    async def test_an_hour_already_covered_is_not_queued_again(self) -> None:
        covered = frozenset({MONDAY + timedelta(hours=2)})
        missing = missing_open_hours(
            MONDAY,
            MONDAY + timedelta(hours=5),
            schedule=WEEKDAYS_ONLY,
            already_covered=covered,
        )
        assert MONDAY + timedelta(hours=2) not in missing
        assert len(missing) == 4

    async def test_a_partly_open_hour_is_queued(self) -> None:
        # The session closes at 23:00 Friday, so the hour from 22:00 is fully open and
        # the hour from 23:00 is not open at all. A partly open hour still holds ticks,
        # and skipping one would leave a real hole at every session boundary.
        friday = MONDAY + timedelta(days=4)
        schedule = TradingSchedule(
            timezone="UTC",
            sessions=(
                WeeklySession(
                    open_day=Weekday.MONDAY,
                    open_time=time(0, 0),
                    close_day=Weekday.FRIDAY,
                    close_time=time(22, 30),
                ),
            ),
        )
        missing = missing_open_hours(
            friday + timedelta(hours=22),
            friday + timedelta(hours=24),
            schedule=schedule,
            already_covered=frozenset(),
        )
        assert missing == (friday + timedelta(hours=22),)

    async def test_a_malformed_window_is_refused(self) -> None:
        with pytest.raises(TradingSysError, match="aligned to the hour"):
            missing_open_hours(
                MONDAY + timedelta(minutes=15),
                MONDAY + timedelta(hours=2),
                schedule=WEEKDAYS_ONLY,
                already_covered=frozenset(),
            )


class TestRecordedHours:
    async def test_any_tick_in_an_hour_marks_that_hour_covered(self) -> None:
        stamps = [
            MONDAY + timedelta(minutes=3),
            MONDAY + timedelta(minutes=59, seconds=59),
            MONDAY + timedelta(hours=2, minutes=30),
        ]
        assert recorded_hours(stamps) == frozenset({MONDAY, MONDAY + timedelta(hours=2)})

    async def test_no_timestamps_cover_nothing(self) -> None:
        assert recorded_hours([]) == frozenset()


class TestTheJob:
    async def test_it_queues_the_open_hours_it_found(self) -> None:
        instrument = eurusd()
        queue = RecordingQueue()
        job = BackfillJob(
            plan_for(instrument.id),
            instrument,
            queue=queue,  # type: ignore[arg-type]
            fetcher=IdleFeed(),
            store=IdleStore(),
        )

        report = await job.run(window_start=MONDAY, window_end=MONDAY + timedelta(hours=4))

        assert report.queued == len(queue.enqueued) > 0
        assert report.open_hours == len(queue.enqueued)

    async def test_a_plan_for_a_different_instrument_is_refused(self) -> None:
        # The schedule comes from the instrument and the queue key from the plan. If
        # they disagree the job queues hours computed against the wrong trading week.
        other = InstrumentId(venue="fxbroker", symbol="GBP/USD")
        with pytest.raises(TradingSysError, match="wrong schedule"):
            BackfillJob(
                plan_for(other),
                eurusd(),
                queue=RecordingQueue(),  # type: ignore[arg-type]
                fetcher=IdleFeed(),
                store=IdleStore(),
            )

    async def test_a_window_with_nothing_open_queues_nothing_and_still_reports(self) -> None:
        instrument = eurusd()
        queue = RecordingQueue()
        job = BackfillJob(
            plan_for(instrument.id),
            instrument,
            queue=queue,  # type: ignore[arg-type]
            fetcher=IdleFeed(),
            store=IdleStore(),
        )

        report = await job.run(
            window_start=SATURDAY,
            window_end=SATURDAY + timedelta(hours=12),
        )

        assert report.queued == 0
        assert queue.enqueued == []
        assert "queued 0" in report.summary()
