"""The backfill queue against real PostgreSQL.

The unit tests drive the runner against a scripted queue, which proves the runner's
handling of the state machine. They cannot prove the state machine, because that lives
in SQL: the atomic claim, the constraints that refuse a complete row with no row count
or a failed row with no reason, and the stale claim recovery that makes a crashed run
resumable rather than a permanent hole.

Those are what these cover, and they need the real database to mean anything.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg
import pytest

from tests.factories import eurusd
from tradingsys.core.errors import PersistenceError
from tradingsys.persistence.backfill import BackfillRepository, BackfillStatus

if TYPE_CHECKING:
    from tradingsys.persistence.database import Database
    from tradingsys.persistence.repositories import InstrumentRepository

pytestmark = pytest.mark.integration

SOURCE = "dukascopy-test"
HOUR = datetime(2026, 8, 10, 13, tzinfo=UTC)
STALE = timedelta(minutes=30)


@pytest.fixture
async def queue(database: Database) -> BackfillRepository:
    await database.execute("DELETE FROM backfill_hours WHERE source = $1", SOURCE)
    return BackfillRepository(database)


@pytest.fixture
async def row_id(instruments: InstrumentRepository) -> int:
    return await instruments.upsert(eurusd())


class TestEnqueue:
    async def test_hours_are_queued_as_pending(
        self, queue: BackfillRepository, row_id: int
    ) -> None:
        hours = [HOUR + timedelta(hours=index) for index in range(3)]
        assert await queue.enqueue(SOURCE, row_id, hours) == 3
        assert (await queue.counts(SOURCE))[BackfillStatus.PENDING] == 3

    async def test_requeueing_does_not_reset_progress(
        self, queue: BackfillRepository, row_id: int
    ) -> None:
        # A caller that recomputes the range it wants and enqueues again must not undo
        # completed work, or a backfill could never converge.
        await queue.enqueue(SOURCE, row_id, [HOUR])
        claimed = await queue.claim(SOURCE, limit=1, stale_after=STALE)
        await queue.mark_complete(claimed[0], rows_written=42)

        await queue.enqueue(SOURCE, row_id, [HOUR])

        counts = await queue.counts(SOURCE)
        assert counts[BackfillStatus.COMPLETE] == 1
        assert counts[BackfillStatus.PENDING] == 0

    async def test_an_unaligned_hour_is_refused_before_it_reaches_the_database(
        self, queue: BackfillRepository, row_id: int
    ) -> None:
        with pytest.raises(PersistenceError, match="not aligned"):
            await queue.enqueue(SOURCE, row_id, [HOUR + timedelta(minutes=30)])

    async def test_a_naive_hour_is_refused(self, queue: BackfillRepository, row_id: int) -> None:
        with pytest.raises(PersistenceError, match="naive"):
            await queue.enqueue(SOURCE, row_id, [datetime(2026, 8, 10, 13)])  # noqa: DTZ001


class TestClaiming:
    async def test_a_claim_takes_pending_hours_in_order(
        self, queue: BackfillRepository, row_id: int
    ) -> None:
        hours = [HOUR + timedelta(hours=index) for index in range(5)]
        await queue.enqueue(SOURCE, row_id, hours)

        claimed = await queue.claim(SOURCE, limit=3, stale_after=STALE)

        assert [item.hour_start for item in claimed] == hours[:3]
        assert all(item.attempts == 1 for item in claimed)

    async def test_two_concurrent_claims_never_take_the_same_hour(
        self, queue: BackfillRepository, row_id: int
    ) -> None:
        # This is the property that makes three concurrent workers safe. Without SKIP
        # LOCKED they would fetch the same hours and spend the feed's tolerance twice.
        hours = [HOUR + timedelta(hours=index) for index in range(6)]
        await queue.enqueue(SOURCE, row_id, hours)

        first, second = await asyncio.gather(
            queue.claim(SOURCE, limit=3, stale_after=STALE),
            queue.claim(SOURCE, limit=3, stale_after=STALE),
        )

        taken = [item.hour_start for item in (*first, *second)]
        assert len(taken) == len(set(taken)), "an hour was claimed twice"
        assert set(taken) <= set(hours)

    async def test_an_in_progress_hour_is_not_reclaimed_while_its_claim_is_fresh(
        self, queue: BackfillRepository, row_id: int
    ) -> None:
        await queue.enqueue(SOURCE, row_id, [HOUR])
        await queue.claim(SOURCE, limit=1, stale_after=STALE)

        again = await queue.claim(SOURCE, limit=1, stale_after=STALE)

        assert again == []

    async def test_a_stale_claim_is_reclaimed_so_a_crash_is_not_a_hole(
        self, queue: BackfillRepository, row_id: int
    ) -> None:
        # A run that died holding an hour must not strand it. Reclaiming after the
        # claim goes stale is what makes the queue drain rather than wedge.
        await queue.enqueue(SOURCE, row_id, [HOUR])
        first = await queue.claim(SOURCE, limit=1, stale_after=STALE)
        assert len(first) == 1

        reclaimed = await queue.claim(SOURCE, limit=1, stale_after=timedelta(seconds=-1))

        assert [item.hour_start for item in reclaimed] == [HOUR]
        assert reclaimed[0].attempts == 2, "a reclaim must count as another attempt"

    async def test_a_failed_hour_is_claimable_again(
        self, queue: BackfillRepository, row_id: int
    ) -> None:
        await queue.enqueue(SOURCE, row_id, [HOUR])
        claimed = await queue.claim(SOURCE, limit=1, stale_after=STALE)
        await queue.mark_failed(claimed[0], error="the feed dropped the connection")

        again = await queue.claim(SOURCE, limit=1, stale_after=STALE)

        assert [item.hour_start for item in again] == [HOUR]
        assert again[0].attempts == 2

    async def test_a_complete_hour_is_never_claimed_again(
        self, queue: BackfillRepository, row_id: int
    ) -> None:
        await queue.enqueue(SOURCE, row_id, [HOUR])
        claimed = await queue.claim(SOURCE, limit=1, stale_after=STALE)
        await queue.mark_complete(claimed[0], rows_written=0)

        assert await queue.claim(SOURCE, limit=5, stale_after=timedelta(seconds=-1)) == []

    async def test_a_non_positive_limit_is_refused(self, queue: BackfillRepository) -> None:
        with pytest.raises(PersistenceError, match="must be positive"):
            await queue.claim(SOURCE, limit=0, stale_after=STALE)


class TestTerminalStates:
    async def test_an_empty_hour_completes_with_zero_rows(
        self, queue: BackfillRepository, row_id: int
    ) -> None:
        # Zero is a real answer, not a failure: the feed publishes nothing for hours the
        # market was closed, and the constraint requires a row count on a complete row
        # precisely so that zero has to be stated rather than left null.
        await queue.enqueue(SOURCE, row_id, [HOUR])
        claimed = await queue.claim(SOURCE, limit=1, stale_after=STALE)

        await queue.mark_complete(claimed[0], rows_written=0)

        assert (await queue.counts(SOURCE))[BackfillStatus.COMPLETE] == 1

    async def test_a_failure_without_a_reason_is_refused(
        self, queue: BackfillRepository, row_id: int
    ) -> None:
        await queue.enqueue(SOURCE, row_id, [HOUR])
        claimed = await queue.claim(SOURCE, limit=1, stale_after=STALE)

        with pytest.raises(PersistenceError, match="must carry a reason"):
            await queue.mark_failed(claimed[0], error="   ")

    async def test_the_database_refuses_a_complete_row_with_no_row_count(
        self, database: Database, queue: BackfillRepository, row_id: int
    ) -> None:
        # Asserted against the constraint itself rather than trusting the repository to
        # always pass a count. The table is the last line of defence for this.
        await queue.enqueue(SOURCE, row_id, [HOUR])
        with pytest.raises(asyncpg.CheckViolationError):
            await database.execute(
                "UPDATE backfill_hours SET status = 'complete', completed_at = now() "
                "WHERE source = $1 AND instrument_id = $2 AND hour_start = $3",
                SOURCE,
                row_id,
                HOUR,
            )

    async def test_the_database_refuses_a_failed_row_with_no_reason(
        self, database: Database, queue: BackfillRepository, row_id: int
    ) -> None:
        await queue.enqueue(SOURCE, row_id, [HOUR])
        with pytest.raises(asyncpg.CheckViolationError):
            await database.execute(
                "UPDATE backfill_hours SET status = 'failed' "
                "WHERE source = $1 AND instrument_id = $2 AND hour_start = $3",
                SOURCE,
                row_id,
                HOUR,
            )

    async def test_the_database_refuses_an_unaligned_hour(
        self, database: Database, row_id: int
    ) -> None:
        with pytest.raises(asyncpg.CheckViolationError):
            await database.execute(
                "INSERT INTO backfill_hours (source, instrument_id, hour_start, status) "
                "VALUES ($1, $2, $3, 'pending')",
                SOURCE,
                row_id,
                HOUR + timedelta(minutes=30),
            )

    async def test_completing_an_hour_clears_a_previous_failure_reason(
        self, database: Database, queue: BackfillRepository, row_id: int
    ) -> None:
        # An hour that failed and later succeeded must not keep advertising the old
        # reason, or an operator reading the queue sees failures that no longer exist.
        await queue.enqueue(SOURCE, row_id, [HOUR])
        claimed = await queue.claim(SOURCE, limit=1, stale_after=STALE)
        await queue.mark_failed(claimed[0], error="a transient timeout")

        again = await queue.claim(SOURCE, limit=1, stale_after=STALE)
        await queue.mark_complete(again[0], rows_written=5)

        stored = await database.fetchrow(
            "SELECT status, rows_written, last_error FROM backfill_hours "
            "WHERE source = $1 AND instrument_id = $2 AND hour_start = $3",
            SOURCE,
            row_id,
            HOUR,
        )
        assert stored is not None
        assert stored["status"] == "complete"
        assert stored["rows_written"] == 5
        assert stored["last_error"] is None
