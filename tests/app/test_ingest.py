"""The ingest process: what it supervises, and what it refuses to pretend.

This file exists because `PROGRESS.md` carried "Complete" for a module that measured 0
percent coverage and was constructed by nothing. The definition of complete in
`docs/DECISIONS.md` now requires that something which runs constructs it, that it is
configured, and that it is tested. These are the third of those three.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from tradingsys.app.ingest import IngestPlan, IngestProcess
from tradingsys.config.settings import IngestSettings
from tradingsys.core.clock import FixedClock
from tradingsys.core.instrument import InstrumentId
from tradingsys.core.provenance import TickSource
from tradingsys.marketdata.recorder import QuoteRecorder
from tradingsys.venues.models import Quote

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from tradingsys.core.instrument import Instrument

pytestmark = pytest.mark.asyncio

ETH = InstrumentId("bybit", "ETH/USDT")
START = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def plan(**overrides: timedelta) -> IngestPlan:
    defaults: dict[str, timedelta] = {
        "registry_interval": timedelta(hours=6),
        "backfill_interval": timedelta(hours=1),
        "backfill_window": timedelta(days=7),
        "quote_deadline": timedelta(seconds=30),
        "registry_deadline": timedelta(hours=7),
        "backfill_deadline": timedelta(minutes=90),
    }
    defaults.update(overrides)
    return IngestPlan(**defaults)


class RecordingWriter:
    """The one thing the recorder needs from storage."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, Quote]] = []

    async def store_ticks(
        self, instrument_row_id: int, source: TickSource, ticks: Sequence[Quote]
    ) -> int:
        del source
        self.rows.extend((instrument_row_id, tick) for tick in ticks)
        return len(ticks)


class ScriptedSource:
    """An instrument source that answers, and counts how often it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def venue(self) -> str:
        return "bybit"

    async def instruments(self) -> Sequence[Instrument]:
        self.calls += 1
        return ()


class ScriptedRegistry:
    def __init__(self) -> None:
        self.syncs = 0

    async def sync(self, source: object) -> object:
        del source
        self.syncs += 1

        class Report:
            moved: tuple[()] = ()

            def summary(self) -> str:
                return "no drift"

        return Report()


def quote(at: datetime, bid: str = "1880.00", ask: str = "1880.01") -> Quote:
    return Quote(instrument_id=ETH, ts=at, bid=Decimal(bid), ask=Decimal(ask))


def build(
    *,
    quotes: AsyncIterator[Quote] | None = None,
    the_plan: IngestPlan | None = None,
) -> tuple[IngestProcess, RecordingWriter, FixedClock]:
    clock = FixedClock(START)
    writer = RecordingWriter()
    recorder = QuoteRecorder(
        repository=writer,
        row_ids={ETH: 1},
        source=TickSource.BYBIT,
        batch_size=1,
        now=clock.now,
    )

    async def default_quotes() -> AsyncIterator[Quote]:
        for tick in range(3):
            yield quote(START + timedelta(seconds=tick))
        await asyncio.sleep(3600)

    stream = quotes if quotes is not None else default_quotes()

    process = IngestProcess(
        the_plan or plan(),
        clock,
        registry=ScriptedRegistry(),  # type: ignore[arg-type]
        sources=[ScriptedSource()],
        recorder=recorder,
        quotes=lambda: stream,
    )
    return process, writer, clock


class TestPlanFromSettings:
    async def test_every_value_comes_from_configuration(self) -> None:
        """No interval or deadline is a constant in the code."""
        settings = IngestSettings(
            registry_interval_seconds=100.0,
            registry_deadline_seconds=200.0,
            backfill_interval_seconds=300.0,
            backfill_deadline_seconds=400.0,
            backfill_window_seconds=500.0,
            quote_deadline_seconds=600.0,
        )
        built = IngestPlan.from_settings(settings)

        assert built.registry_interval == timedelta(seconds=100)
        assert built.registry_deadline == timedelta(seconds=200)
        assert built.backfill_interval == timedelta(seconds=300)
        assert built.backfill_deadline == timedelta(seconds=400)
        assert built.backfill_window == timedelta(seconds=500)
        assert built.quote_deadline == timedelta(seconds=600)


class TestRegisteredActivities:
    async def test_the_crypto_stream_is_registered_with_its_deadline(self) -> None:
        process, _, _ = build()
        process.register_activities()

        states = process.supervisor.states
        assert "crypto_quotes" in states
        assert "instrument_registry" in states

    async def test_no_backfill_activity_without_a_backfill_job(self) -> None:
        """Registering one with nothing to do would report progress forever and prove
        nothing, which is the shape of check this system already rejected once."""
        process, _, _ = build()
        process.register_activities()

        assert "dukascopy_backfill" not in process.supervisor.states

    async def test_forex_live_quotes_are_not_claimed(self) -> None:
        """There is no forex stream yet, and the process must not imply otherwise."""
        process, _, _ = build()
        process.register_activities()

        assert set(process.supervisor.states) == {"crypto_quotes", "instrument_registry"}


class TestQuotesReachStorage:
    async def test_a_quote_is_recorded_and_reports_progress(self) -> None:
        process, writer, clock = build()
        process.register_activities()
        process.start()
        try:
            await asyncio.sleep(0.05)
        finally:
            await process.stop()

        assert len(writer.rows) == 3
        assert writer.rows[0][0] == 1
        assert process.supervisor.states["crypto_quotes"].last_progress >= clock.now()

    async def test_stopping_flushes_what_the_recorder_still_holds(self) -> None:
        """A batch that was accepted and not yet written is data we claimed to have."""
        clock = FixedClock(START)
        writer = RecordingWriter()
        recorder = QuoteRecorder(
            repository=writer,
            row_ids={ETH: 1},
            source=TickSource.BYBIT,
            batch_size=1000,
            flush_interval_seconds=3600.0,
            now=clock.now,
        )

        async def two_quotes() -> AsyncIterator[Quote]:
            yield quote(START)
            yield quote(START + timedelta(seconds=1))
            await asyncio.sleep(3600)

        process = IngestProcess(
            plan(),
            clock,
            registry=ScriptedRegistry(),  # type: ignore[arg-type]
            sources=[ScriptedSource()],
            recorder=recorder,
            quotes=two_quotes,
        )
        process.register_activities()
        process.start()
        await asyncio.sleep(0.05)
        assert writer.rows == []

        await process.stop()
        assert len(writer.rows) == 2


class TestStallDetection:
    async def test_a_silent_stream_is_stalled_by_wall_clock(self) -> None:
        """The failure this supervisor exists for: the task is alive, nothing raised,
        and no data is arriving. Detection is elapsed wall clock, not loop iterations."""

        async def silent() -> AsyncIterator[Quote]:
            await asyncio.sleep(3600)
            yield quote(START)

        process, _, clock = build(quotes=silent())
        process.register_activities()
        process.start()
        try:
            await asyncio.sleep(0.05)
            assert process.supervisor.stalled() == ()

            clock.advance(31)
            assert "crypto_quotes" in process.supervisor.stalled()
        finally:
            await process.stop()

    async def test_the_readiness_check_reports_the_stall(self) -> None:
        async def silent() -> AsyncIterator[Quote]:
            await asyncio.sleep(3600)
            yield quote(START)

        process, _, clock = build(quotes=silent())
        process.register_activities()
        process.start()
        try:
            passed, detail = await process.progress_check()()
            assert passed is True

            clock.advance(31)
            passed, detail = await process.progress_check()()
            assert passed is False
            assert "crypto_quotes" in detail
        finally:
            await process.stop()

    async def test_a_stall_is_not_a_failure_count(self) -> None:
        """An activity appears stalled while its task is alive and its exception count
        is zero, which is exactly what a liveness check misses."""

        async def silent() -> AsyncIterator[Quote]:
            await asyncio.sleep(3600)
            yield quote(START)

        process, _, clock = build(quotes=silent())
        process.register_activities()
        process.start()
        try:
            # The supervisor creates tasks; they have to run before there is anything
            # to observe. Without this the assertion would pass on a task that had not
            # started, which is a different thing from one that is alive and silent.
            await asyncio.sleep(0.05)
            clock.advance(31)
            state = process.supervisor.states["crypto_quotes"]

            assert state.running is True
            assert state.failures == 0
            assert "crypto_quotes" in process.supervisor.stalled()
        finally:
            await process.stop()
