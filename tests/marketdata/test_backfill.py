"""The backfill runner: resumability, retries, and the states an hour can end in.

What matters here is not that a good hour is stored. It is that no hour can end a run
in a state nobody will look at again: not silently skipped, not marked complete without
its rows, and not left claimed forever by a process that died holding it.

The doubles are hand written. A scripted feed can be made to serve an HTML error page,
fail twice then succeed, or hold nothing at all, which is what the failure paths need
and what a recorded fixture cannot be talked into doing.
"""

from __future__ import annotations

import lzma
import random
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from tradingsys.core.errors import TradingSysError
from tradingsys.core.instrument import InstrumentId
from tradingsys.core.provenance import TickSource
from tradingsys.marketdata.backfill import (
    BackfillPlan,
    BackfillRunner,
    hours_between,
)
from tradingsys.marketdata.dukascopy import decode_hour, hour_url
from tradingsys.persistence.backfill import BackfillHour

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tradingsys.venues.models import Quote

pytestmark = pytest.mark.asyncio

INSTRUMENT = InstrumentId(venue="dukascopy", symbol="EUR/USD")
SOURCE = "dukascopy"
ROW_ID = 7
HOUR = datetime(2026, 8, 10, 13, tzinfo=UTC)


def bi5_payload(count: int, *, digits: int = 5) -> bytes:
    """A real .bi5 body: LZMA alone framing over twenty byte big endian records.

    The field order is the feed's own: milliseconds into the hour, then **ask**, then
    bid, then ask volume, then bid volume. Writing it bid first produces crossed quotes
    on every record, which the decoder faithfully reports as crossed rather than
    silently reordering, so getting this wrong here fails loudly. It did.
    """
    del digits
    body = b"".join(
        struct.pack(">IIIff", index * 1000, 110010 + index, 110000 + index, 2.5, 1.5)
        for index in range(count)
    )
    compressor = lzma.LZMACompressor(format=lzma.FORMAT_ALONE)
    return compressor.compress(body) + compressor.flush()


@dataclass(slots=True)
class ScriptedFeed:
    """A feed that can be told to fail, serve rubbish, or hold nothing.

    Attributes:
        bodies: Payload per URL. A URL absent from it holds nothing, which is what the
            real feed does for every weekend hour.
        failures_before_success: How many times each URL raises before it answers.
        always_fails: URLs that never answer, for the path that ends in a failed hour.
    """

    bodies: dict[str, bytes] = field(default_factory=dict)
    failures_before_success: dict[str, int] = field(default_factory=dict)
    always_fails: set[str] = field(default_factory=set)
    attempts: dict[str, int] = field(default_factory=dict)

    async def fetch(self, url: str) -> bytes | None:
        self.attempts[url] = self.attempts.get(url, 0) + 1
        if url in self.always_fails:
            raise ConnectionResetError("the scripted feed dropped the connection")
        remaining = self.failures_before_success.get(url, 0)
        if remaining >= self.attempts[url]:
            raise TimeoutError("the scripted feed timed out")
        return self.bodies.get(url)


@dataclass(slots=True)
class ScriptedQueue:
    """An in memory stand in for `backfill_hours`, with the same lifecycle rules.

    It is not a reimplementation of the SQL. It holds the state machine so the runner's
    handling of it can be exercised without a database; the SQL itself is covered by the
    integration suite against real PostgreSQL.
    """

    pending: list[datetime] = field(default_factory=list)
    complete: dict[datetime, int] = field(default_factory=dict)
    failed: dict[datetime, str] = field(default_factory=dict)
    claims: int = 0

    async def claim(
        self, source: str, *, limit: int, stale_after: timedelta
    ) -> Sequence[BackfillHour]:
        del source, stale_after
        self.claims += 1
        taken = self.pending[:limit]
        self.pending = self.pending[limit:]
        return [
            BackfillHour(source=SOURCE, instrument_row_id=ROW_ID, hour_start=hour, attempts=1)
            for hour in taken
        ]

    async def mark_complete(self, hour: BackfillHour, *, rows_written: int) -> None:
        if hour.hour_start in self.failed:
            raise AssertionError("an hour was marked complete after being marked failed")
        self.complete[hour.hour_start] = rows_written

    async def mark_failed(self, hour: BackfillHour, *, error: str) -> None:
        if not error.strip():
            raise AssertionError("an hour was failed with no reason")
        self.failed[hour.hour_start] = error


@dataclass(slots=True)
class RecordingStore:
    written: list[tuple[int, TickSource, int]] = field(default_factory=list)
    quotes: list[Quote] = field(default_factory=list)

    async def store_ticks(
        self, instrument_row_id: int, source: TickSource, ticks: Sequence[Quote]
    ) -> int:
        self.written.append((instrument_row_id, source, len(ticks)))
        self.quotes.extend(ticks)
        return len(ticks)


def plan(**overrides: object) -> BackfillPlan:
    arguments: dict[str, object] = {
        "source": SOURCE,
        "instrument_id": INSTRUMENT,
        "instrument_row_id": ROW_ID,
        "venue_symbol": "EURUSD",
        "digits": 5,
        "concurrency": 3,
        "max_attempts_per_hour": 3,
        "backoff_seconds": 0.001,
        "stale_claim_after": timedelta(minutes=30),
    }
    arguments.update(overrides)
    return BackfillPlan(**arguments)  # type: ignore[arg-type]


def build(
    queue: ScriptedQueue, feed: ScriptedFeed, store: RecordingStore, **overrides: object
) -> BackfillRunner:
    return BackfillRunner(
        plan(**overrides),
        queue=queue,  # type: ignore[arg-type]
        fetcher=feed,
        store=store,
        jitter=random.Random(0),
    )


class TestAnHourAlwaysReachesATerminalState:
    async def test_a_good_hour_is_stored_and_marked_complete_with_its_row_count(self) -> None:
        queue = ScriptedQueue(pending=[HOUR])
        feed = ScriptedFeed(bodies={_url(HOUR): bi5_payload(4)})
        store = RecordingStore()

        stats = await build(queue, feed, store).run()

        assert queue.complete == {HOUR: 4}
        assert queue.failed == {}
        assert stats.completed == 1
        assert stats.rows_written == 4
        assert store.written == [(ROW_ID, TickSource.DUKASCOPY, 4)]

    async def test_an_hour_the_feed_holds_nothing_for_is_complete_with_zero_rows(self) -> None:
        # Every weekend hour is one of these. Recording it as complete is what stops the
        # backfill asking for it forever; recording it as failed would never converge.
        queue = ScriptedQueue(pending=[HOUR])
        feed = ScriptedFeed(bodies={})
        store = RecordingStore()

        stats = await build(queue, feed, store).run()

        assert queue.complete == {HOUR: 0}
        assert stats.empty_hours == 1
        assert store.written == [], "an empty hour must not reach the tick table"

    async def test_an_hour_that_never_fetches_is_failed_with_its_reason(self) -> None:
        queue = ScriptedQueue(pending=[HOUR])
        feed = ScriptedFeed(always_fails={_url(HOUR)})
        store = RecordingStore()

        stats = await build(queue, feed, store).run()

        assert HOUR not in queue.complete
        assert "ConnectionResetError" in queue.failed[HOUR]
        assert stats.failed == 1
        assert stats.last_error is not None

    async def test_a_failed_hour_is_never_marked_complete(self) -> None:
        # The scripted queue raises if this is violated, which makes the ordering rule
        # a property of the test rather than something a reader has to check by eye.
        queue = ScriptedQueue(pending=[HOUR])
        feed = ScriptedFeed(always_fails={_url(HOUR)})
        await build(queue, feed, RecordingStore()).run()
        assert queue.complete == {}

    async def test_every_claimed_hour_ends_claimed_by_nothing(self) -> None:
        hours = [HOUR + timedelta(hours=index) for index in range(6)]
        queue = ScriptedQueue(pending=list(hours))
        feed = ScriptedFeed(
            bodies={_url(hours[0]): bi5_payload(2), _url(hours[2]): bi5_payload(1)},
            always_fails={_url(hours[4])},
        )

        stats = await build(queue, feed, RecordingStore()).run()

        settled = set(queue.complete) | set(queue.failed)
        assert settled == set(hours), "an hour was claimed and left in no terminal state"
        assert stats.claimed == len(hours)
        assert stats.completed + stats.failed == len(hours)


class TestRetries:
    async def test_a_transport_failure_is_retried_and_can_succeed(self) -> None:
        queue = ScriptedQueue(pending=[HOUR])
        feed = ScriptedFeed(
            bodies={_url(HOUR): bi5_payload(2)}, failures_before_success={_url(HOUR): 2}
        )

        stats = await build(queue, feed, RecordingStore()).run()

        assert queue.complete == {HOUR: 2}
        assert feed.attempts[_url(HOUR)] == 3
        assert stats.retries == 2

    async def test_retries_are_bounded_and_the_hour_is_left_for_a_later_run(self) -> None:
        queue = ScriptedQueue(pending=[HOUR])
        feed = ScriptedFeed(always_fails={_url(HOUR)})

        await build(queue, feed, RecordingStore(), max_attempts_per_hour=2).run()

        assert feed.attempts[_url(HOUR)] == 2
        assert HOUR in queue.failed

    async def test_a_corrupt_payload_is_not_retried(self) -> None:
        # The reader refuses an HTML error page rather than decoding it as an empty
        # hour. Asking again in a second does not make it valid, and retrying would turn
        # a clear signal about the feed into a slow one.
        queue = ScriptedQueue(pending=[HOUR])
        feed = ScriptedFeed(bodies={_url(HOUR): b"<html>rate limited</html>"})

        stats = await build(queue, feed, RecordingStore()).run()

        assert feed.attempts[_url(HOUR)] == 1
        assert "Bi5DecodeError" in queue.failed[HOUR]
        assert stats.retries == 0


class TestResumability:
    async def test_a_second_run_picks_up_what_the_first_left_failed(self) -> None:
        queue = ScriptedQueue(pending=[HOUR])
        feed = ScriptedFeed(always_fails={_url(HOUR)})
        await build(queue, feed, RecordingStore()).run()
        assert HOUR in queue.failed

        # The real queue reclaims failed hours; this reproduces that by requeueing, then
        # gives the feed the data it was missing.
        queue.pending = [HOUR]
        queue.failed.clear()
        feed.always_fails.clear()
        feed.bodies[_url(HOUR)] = bi5_payload(3)

        stats = await build(queue, feed, RecordingStore()).run()

        assert queue.complete == {HOUR: 3}
        assert stats.failed == 0

    async def test_an_empty_queue_is_a_successful_run(self) -> None:
        stats = await build(ScriptedQueue(), ScriptedFeed(), RecordingStore()).run()
        assert stats.claimed == 0
        assert stats.completed == 0
        assert stats.failed == 0

    async def test_a_bounded_run_stops_at_the_limit_and_leaves_the_rest(self) -> None:
        hours = [HOUR + timedelta(hours=index) for index in range(10)]
        queue = ScriptedQueue(pending=list(hours))
        feed = ScriptedFeed(bodies={_url(hour): bi5_payload(1) for hour in hours})

        stats = await build(queue, feed, RecordingStore()).run(max_hours=4)

        assert stats.claimed == 4
        assert len(queue.pending) == 6


class TestConcurrency:
    async def test_no_more_than_the_configured_hours_are_claimed_at_once(self) -> None:
        hours = [HOUR + timedelta(hours=index) for index in range(9)]
        queue = ScriptedQueue(pending=list(hours))
        feed = ScriptedFeed(bodies={_url(hour): bi5_payload(1) for hour in hours})

        await build(queue, feed, RecordingStore(), concurrency=3).run()

        # Nine hours at three per claim is three claims, then one that comes back empty.
        assert queue.claims == 4
        assert len(queue.complete) == 9

    async def test_one_failing_hour_does_not_abandon_the_others_beside_it(self) -> None:
        # An exception escaping a worker would leave its siblings in the same gather
        # unfinished and claimed, which is a slow silent way to lose them.
        hours = [HOUR + timedelta(hours=index) for index in range(3)]
        queue = ScriptedQueue(pending=list(hours))
        feed = ScriptedFeed(
            bodies={_url(hours[0]): bi5_payload(1), _url(hours[2]): bi5_payload(1)},
            always_fails={_url(hours[1])},
        )

        await build(queue, feed, RecordingStore()).run()

        assert set(queue.complete) == {hours[0], hours[2]}
        assert set(queue.failed) == {hours[1]}


class TestThePlanRefusesNonsense:
    @pytest.mark.parametrize(
        ("field_name", "value"),
        [("concurrency", 0), ("max_attempts_per_hour", 0), ("backoff_seconds", 0.0)],
    )
    async def test_a_non_positive_setting_is_refused(self, field_name: str, value: object) -> None:
        with pytest.raises(TradingSysError):
            plan(**{field_name: value})


class TestHoursBetween:
    async def test_it_is_half_open(self) -> None:
        start = datetime(2026, 8, 10, 0, tzinfo=UTC)
        end = datetime(2026, 8, 10, 3, tzinfo=UTC)
        assert hours_between(start, end) == (
            start,
            start + timedelta(hours=1),
            start + timedelta(hours=2),
        )

    async def test_an_empty_range_is_empty_rather_than_an_error(self) -> None:
        instant = datetime(2026, 8, 10, 0, tzinfo=UTC)
        assert hours_between(instant, instant) == ()

    async def test_a_naive_bound_is_refused(self) -> None:
        with pytest.raises(TradingSysError, match="timezone aware"):
            hours_between(datetime(2026, 8, 10, 0), datetime(2026, 8, 10, 1, tzinfo=UTC))  # noqa: DTZ001

    async def test_an_unaligned_bound_is_refused(self) -> None:
        # A loosely expressed range produces a queue quietly missing an hour at one end.
        with pytest.raises(TradingSysError, match="aligned to the hour"):
            hours_between(
                datetime(2026, 8, 10, 0, 30, tzinfo=UTC), datetime(2026, 8, 10, 3, tzinfo=UTC)
            )

    async def test_a_reversed_range_is_refused(self) -> None:
        with pytest.raises(TradingSysError, match="is before start"):
            hours_between(
                datetime(2026, 8, 10, 5, tzinfo=UTC), datetime(2026, 8, 10, 1, tzinfo=UTC)
            )


def _url(hour: datetime) -> str:
    return hour_url("EURUSD", hour)


async def test_the_quotes_carry_the_decoded_prices() -> None:
    """Prices reach the store scaled, not as the archive's raw integers."""
    ticks = decode_hour(bi5_payload(1), hour=HOUR, digits=5)
    assert ticks[0].bid == Decimal("1.10000")
    assert ticks[0].ask == Decimal("1.10010")
