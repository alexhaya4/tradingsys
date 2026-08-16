"""Tests for the quote recorder.

The repository is a double here, because what is being tested is the batching and the
loss behaviour, not SQL. What it must never do is lose a quote it accepted: this venue
publishes no historical quote data, so a dropped batch leaves a hole that cannot be
backfilled and cannot be distinguished afterwards from a market that was quiet.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from tradingsys.core.errors import DomainError
from tradingsys.core.instrument import InstrumentId
from tradingsys.core.provenance import TickSource
from tradingsys.marketdata.recorder import QuoteRecorder
from tradingsys.venues.models import Quote

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

BTC = InstrumentId(venue="bybit", symbol="BTC/USDT")
ETH = InstrumentId(venue="bybit", symbol="ETH/USDT")
ROW_IDS = {BTC: 1, ETH: 2}
START = datetime(2026, 8, 16, 12, tzinfo=UTC)


def quote(index: int = 0, instrument_id: InstrumentId = BTC) -> Quote:
    return Quote(
        instrument_id=instrument_id,
        ts=START + timedelta(milliseconds=index),
        bid=Decimal("63033.3"),
        ask=Decimal("63033.4"),
        bid_size=Decimal("1.5"),
        ask_size=Decimal("2.5"),
    )


async def stream_of(*quotes: Quote) -> AsyncIterator[Quote]:
    for item in quotes:
        yield item


class FakeRepository:
    """Records what was asked of it, and can be told to fail."""

    def __init__(self, *, failures: int = 0) -> None:
        self.writes: list[tuple[int, TickSource, int]] = []
        self.rows: list[Quote] = []
        self._failures = failures

    async def store_ticks(
        self, instrument_row_id: int, source: TickSource, ticks: Sequence[Quote]
    ) -> int:
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("connection reset")
        self.writes.append((instrument_row_id, source, len(ticks)))
        self.rows.extend(ticks)
        return len(ticks)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def _no_sleep(seconds: float) -> None:
    """Retry delays are not what these tests are timing."""


def recorder(repository: Any, **kwargs: Any) -> QuoteRecorder:
    kwargs.setdefault("sleep", _no_sleep)
    return QuoteRecorder(repository, ROW_IDS, TickSource.BYBIT, **kwargs)


class TestBatching:
    async def test_a_full_batch_is_written(self) -> None:
        repository = FakeRepository()
        subject = recorder(repository, batch_size=3)
        await subject.run(stream_of(*(quote(index) for index in range(3))))
        assert repository.writes == [(1, TickSource.BYBIT, 3)]

    async def test_a_partial_batch_is_written_when_the_stream_ends(self) -> None:
        repository = FakeRepository()
        subject = recorder(repository, batch_size=100)
        await subject.run(stream_of(quote(0), quote(1)))
        assert repository.writes == [(1, TickSource.BYBIT, 2)]
        assert subject.stats.written == 2

    async def test_an_old_batch_flushes_before_it_fills(self) -> None:
        # A quiet instrument must not sit in memory until the next tick, because the
        # bound on what a crash costs is exactly this interval.
        clock = Clock()
        repository = FakeRepository()
        subject = recorder(repository, batch_size=1000, flush_interval_seconds=5, now=clock)

        async def slow() -> AsyncIterator[Quote]:
            yield quote(0)
            clock.advance(6)
            yield quote(1)
            yield quote(2)

        await subject.run(slow())
        assert [write[2] for write in repository.writes] == [2, 1]

    async def test_instruments_are_written_separately(self) -> None:
        # Two instruments share the buffer window but not the row, and mixing them
        # would attribute one instrument's quotes to the other.
        repository = FakeRepository()
        subject = recorder(repository, batch_size=2)
        await subject.run(stream_of(quote(0, BTC), quote(1, ETH), quote(2, BTC)))
        assert sorted(write[0] for write in repository.writes) == [1, 2]

    async def test_every_row_carries_the_source(self) -> None:
        # The provenance tag is what keeps research data out of the cost model, so it
        # is written on every row rather than inferred later.
        repository = FakeRepository()
        await recorder(repository).run(stream_of(quote(0)))
        assert {write[1] for write in repository.writes} == {TickSource.BYBIT}

    async def test_nothing_is_written_for_an_empty_stream(self) -> None:
        repository = FakeRepository()
        subject = recorder(repository)
        await subject.run(stream_of())
        assert repository.writes == []
        assert subject.stats.flushes == 0


class TestNotLosingData:
    async def test_a_failed_write_is_retried_with_the_same_quotes(self) -> None:
        repository = FakeRepository(failures=2)
        subject = recorder(repository, batch_size=2, write_attempts=3)
        await subject.run(stream_of(quote(0), quote(1)))
        assert repository.writes == [(1, TickSource.BYBIT, 2)]
        assert subject.stats.write_failures == 2
        assert subject.stats.written == 2

    async def test_it_stops_loudly_rather_than_dropping_quotes(self) -> None:
        # The alternative is a recorder that keeps running with a hole in the archive
        # that nothing downstream can detect.
        repository = FakeRepository(failures=99)
        subject = recorder(repository, batch_size=2, write_attempts=3)
        with pytest.raises(RuntimeError, match="connection reset"):
            await subject.run(stream_of(quote(0), quote(1)))
        assert subject.stats.written == 0

    async def test_cancellation_writes_the_batch_in_hand(self) -> None:
        # Shutdown is the normal way this stops, and whatever is buffered at that
        # moment is real data that no backfill can recover.
        repository = FakeRepository()
        subject = recorder(repository, batch_size=1000)
        started = asyncio.Event()

        async def forever() -> AsyncIterator[Quote]:
            yield quote(0)
            yield quote(1)
            started.set()
            await asyncio.sleep(3600)
            yield quote(2)

        task = asyncio.create_task(subject.run(forever()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert repository.writes == [(1, TickSource.BYBIT, 2)]

    async def test_cancellation_still_cancels(self) -> None:
        # A recorder that swallowed the cancellation to save its batch could not be
        # shut down at all.
        repository = FakeRepository(failures=99)
        subject = recorder(repository, batch_size=1000, write_attempts=1)
        started = asyncio.Event()

        async def forever() -> AsyncIterator[Quote]:
            yield quote(0)
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(subject.run(forever()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestUnregisteredInstruments:
    async def test_quotes_for_an_unknown_instrument_are_counted(self) -> None:
        # There is no row to write them to. Counting them per symbol makes a
        # misconfigured subscription visible immediately instead of at month end.
        repository = FakeRepository()
        subject = QuoteRecorder(repository, {BTC: 1}, TickSource.BYBIT, batch_size=2)
        await subject.run(stream_of(quote(0, ETH), quote(1, ETH)))
        assert subject.stats.unknown_instruments == {"bybit:ETH/USDT": 2}
        assert repository.writes == []

    async def test_known_instruments_are_unaffected(self) -> None:
        repository = FakeRepository()
        subject = QuoteRecorder(repository, {BTC: 1}, TickSource.BYBIT, batch_size=4)
        await subject.run(stream_of(quote(0, ETH), quote(1, BTC)))
        assert repository.writes == [(1, TickSource.BYBIT, 1)]
        assert subject.stats.unknown_instruments == {"bybit:ETH/USDT": 1}


class TestStats:
    async def test_the_counters_track_the_stream(self) -> None:
        repository = FakeRepository()
        clock = Clock()
        subject = recorder(repository, batch_size=2, now=clock)
        await subject.run(stream_of(*(quote(index) for index in range(5))))
        assert subject.stats.received == 5
        assert subject.stats.written == 5
        assert subject.stats.flushes == 3
        assert subject.stats.buffered == 0
        assert subject.stats.last_write_at == START

    async def test_buffered_is_the_gap_between_received_and_written(self) -> None:
        repository = FakeRepository()
        subject = recorder(repository, batch_size=100)
        async for item in stream_of(quote(0), quote(1)):
            subject.stats.received += 1
            assert item is not None
        assert subject.stats.buffered == 2


class TestConstruction:
    @pytest.mark.parametrize("size", [0, -1])
    def test_a_non_positive_batch_size_is_refused(self, size: int) -> None:
        with pytest.raises(DomainError, match="batch_size must be positive"):
            QuoteRecorder(FakeRepository(), ROW_IDS, TickSource.BYBIT, batch_size=size)

    def test_a_non_positive_attempt_count_is_refused(self) -> None:
        with pytest.raises(DomainError, match="write_attempts must be positive"):
            QuoteRecorder(FakeRepository(), ROW_IDS, TickSource.BYBIT, write_attempts=0)

    def test_a_negative_flush_interval_is_refused(self) -> None:
        with pytest.raises(DomainError, match="flush_interval_seconds must not be negative"):
            QuoteRecorder(FakeRepository(), ROW_IDS, TickSource.BYBIT, flush_interval_seconds=-1)

    def test_a_non_positive_retry_backoff_is_refused(self) -> None:
        with pytest.raises(DomainError, match="retry_backoff_seconds must be positive"):
            QuoteRecorder(FakeRepository(), ROW_IDS, TickSource.BYBIT, retry_backoff_seconds=0)

    async def test_the_default_sleep_is_the_real_one(self) -> None:
        # Exercises the production path once, so the injected sleep everywhere else is
        # not covering for a default that does not work. The backoff is turned right
        # down so this costs milliseconds rather than seconds.
        repository = FakeRepository(failures=1)
        subject = QuoteRecorder(
            repository,
            ROW_IDS,
            TickSource.BYBIT,
            batch_size=1,
            retry_backoff_seconds=0.001,
        )
        await subject.run(stream_of(quote(0)))
        assert subject.stats.written == 1
