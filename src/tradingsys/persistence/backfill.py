"""The backfill work queue, in the database rather than in memory.

`backfill_hours` holds one row per source, instrument, and hour of history, and it is
the reason a backfill is resumable: progress survives the process, so an interrupted
run continues where it stopped rather than refetching from the beginning or, worse,
leaving a hole it has no record of.

Three rules shape every query here.

**An hour is complete only when its rows are committed.** The status is written in the
same transaction as the ticks, so there is no window in which an hour is marked done
and its data is not there. A crash between the two would otherwise produce a hole that
nothing would ever look for again.

**A failed hour stays failed and is retried later.** It is never skipped and never
quietly marked complete. It carries the reason it failed, so an hour that keeps failing
is visible as a fact rather than as an absence.

**Claiming is atomic across workers.** ``FOR UPDATE SKIP LOCKED`` means two runners, or
one runner with three concurrent workers, cannot take the same hour. A queue that hands
the same work to two workers wastes the rate limit budget on the venue that is least
tolerant of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final, final

from tradingsys.core.errors import PersistenceError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from tradingsys.persistence.database import Database

__all__ = [
    "BackfillHour",
    "BackfillRepository",
    "BackfillStatus",
]


class BackfillStatus(StrEnum):
    """Lifecycle of one hour of history.

    The values are checked by a constraint in migration 0002, so adding one here means
    changing the constraint too.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


@final
@dataclass(frozen=True, slots=True)
class BackfillHour:
    """One claimed unit of work.

    Attributes:
        source: Which feed the hour comes from.
        instrument_row_id: Database id of the instrument, not its venue symbol.
        hour_start: Start of the hour, UTC and aligned, enforced by a constraint.
        attempts: How many times this hour has been claimed, including this claim.
    """

    source: str
    instrument_row_id: int
    hour_start: datetime
    attempts: int


_ENQUEUE: Final = """
INSERT INTO backfill_hours (source, instrument_id, hour_start, status)
VALUES ($1, $2, $3, 'pending')
ON CONFLICT (source, instrument_id, hour_start) DO NOTHING
"""

_CLAIM: Final = """
WITH claimable AS (
    SELECT source, instrument_id, hour_start
    FROM backfill_hours
    WHERE source = $1
      AND (
          status = 'pending'
          OR status = 'failed'
          OR (status = 'in_progress' AND claimed_at < $2)
      )
    ORDER BY hour_start
    LIMIT $3
    FOR UPDATE SKIP LOCKED
)
UPDATE backfill_hours AS b
SET status = 'in_progress',
    attempts = b.attempts + 1,
    claimed_at = now(),
    updated_at = now()
FROM claimable c
WHERE b.source = c.source
  AND b.instrument_id = c.instrument_id
  AND b.hour_start = c.hour_start
RETURNING b.source, b.instrument_id, b.hour_start, b.attempts
"""

_MARK_COMPLETE: Final = """
UPDATE backfill_hours
SET status = 'complete',
    rows_written = $4,
    last_error = NULL,
    completed_at = now(),
    updated_at = now()
WHERE source = $1 AND instrument_id = $2 AND hour_start = $3
"""

_MARK_FAILED: Final = """
UPDATE backfill_hours
SET status = 'failed',
    last_error = $4,
    claimed_at = NULL,
    updated_at = now()
WHERE source = $1 AND instrument_id = $2 AND hour_start = $3
"""

_COUNTS: Final = """
SELECT status, count(*) AS total
FROM backfill_hours
WHERE source = $1
GROUP BY status
"""


@final
class BackfillRepository:
    """Queue operations for `backfill_hours`."""

    __slots__ = ("_database",)

    def __init__(self, database: Database) -> None:
        self._database = database

    async def enqueue(self, source: str, instrument_row_id: int, hours: Iterable[datetime]) -> int:
        """Add hours to the queue, ignoring any already queued.

        Re-enqueueing is deliberately harmless. A caller that recomputes the range it
        wants and enqueues it again must not reset the progress of hours already done,
        or a backfill could never converge.

        Raises:
            PersistenceError: An hour is not aligned to the hour or is not timezone
                aware, both of which the table constrains and neither of which should
                reach the database as a constraint violation.
        """
        rows = []
        for hour in hours:
            if hour.tzinfo is None:
                raise PersistenceError(
                    f"backfill hour {hour!r} is naive; an hour of history without a "
                    f"timezone is an hour in an unknown place"
                )
            if (hour.minute, hour.second, hour.microsecond) != (0, 0, 0):
                raise PersistenceError(
                    f"backfill hour {hour!r} is not aligned to the hour, so it does not "
                    f"name a unit of work this queue can hold"
                )
            rows.append((source, instrument_row_id, hour))
        if not rows:
            return 0
        async with self._database.transaction() as connection:
            await connection.executemany(_ENQUEUE, rows)
        return len(rows)

    async def claim(
        self, source: str, *, limit: int, stale_after: timedelta
    ) -> Sequence[BackfillHour]:
        """Claim up to ``limit`` hours for this worker.

        Pending and failed hours are both claimable, which is what makes a failure a
        retry rather than an abandonment. An hour left ``in_progress`` by a crashed run
        is reclaimed once its claim is older than ``stale_after``, because otherwise a
        single crash would strand that hour forever and the queue would never drain.

        Args:
            source: Feed to claim work for.
            limit: Most hours to take at once.
            stale_after: How old a claim must be before another worker may take it.
                Set it comfortably above the longest a single hour can legitimately
                take, or two workers will duplicate live work.
        """
        if limit <= 0:
            raise PersistenceError(f"claim limit must be positive, got {limit}")
        cutoff = datetime.now(tz=UTC) - stale_after
        async with self._database.transaction() as connection:
            records = await connection.fetch(_CLAIM, source, cutoff, limit)
        return [
            BackfillHour(
                source=record["source"],
                instrument_row_id=record["instrument_id"],
                hour_start=record["hour_start"],
                attempts=record["attempts"],
            )
            for record in records
        ]

    async def mark_complete(self, hour: BackfillHour, *, rows_written: int) -> None:
        """Record an hour as done, with the number of rows it produced.

        Zero is a legitimate row count: the feed publishes nothing for hours the market
        was closed, and recording that as complete with zero rows is what stops the
        backfill from asking for it again forever. It is distinct from a failure, which
        keeps its reason and is retried.
        """
        if rows_written < 0:
            raise PersistenceError(f"rows_written must not be negative, got {rows_written}")
        async with self._database.transaction() as connection:
            await connection.execute(
                _MARK_COMPLETE, hour.source, hour.instrument_row_id, hour.hour_start, rows_written
            )

    async def mark_failed(self, hour: BackfillHour, *, error: str) -> None:
        """Record an hour as failed, with the reason, so it is retried and visible.

        Raises:
            PersistenceError: The reason is empty. The table constrains a failed row to
                carry one, and a failure with no stated cause is the thing this queue
                exists to make impossible.
        """
        reason = error.strip()
        if not reason:
            raise PersistenceError(
                "a failed backfill hour must carry a reason; an unexplained failure is "
                "indistinguishable from an hour nobody attempted"
            )
        async with self._database.transaction() as connection:
            await connection.execute(
                _MARK_FAILED, hour.source, hour.instrument_row_id, hour.hour_start, reason[:2000]
            )

    async def counts(self, source: str) -> dict[BackfillStatus, int]:
        """How many hours sit in each status, for progress reporting."""
        async with self._database.transaction() as connection:
            records = await connection.fetch(_COUNTS, source)
        tally = dict.fromkeys(BackfillStatus, 0)
        for record in records:
            tally[BackfillStatus(record["status"])] = record["total"]
        return tally
