"""Gap detection, and the policy that decides what happens to what it finds.

`SPEC.md` section 8 makes gap detection proven by deliberate disconnection an exit
criterion. The capability was a tested library nothing called, so the tests that matter
here are about the policy rather than the interval arithmetic, which `test_gaps.py`
already covers.

The rule: repairable gaps are queued, permanent ones are recorded, and neither fails
readiness, because a permanent crypto gap pinning readiness red forever would teach an
operator to ignore the one signal that matters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from tests.factories import btcusdt, eurusd
from tradingsys.app.gapcheck import GapMonitor, hours_covering
from tradingsys.config.settings import IngestSettings, InstrumentRef, UniverseSettings
from tradingsys.core.clock import FixedClock
from tradingsys.core.provenance import TickSource
from tradingsys.marketdata.gaps import Gap

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from tradingsys.core.instrument import Instrument, InstrumentId

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


class FakeMarketData:
    """Returns whatever coverage the test supplies, and records what it was asked."""

    def __init__(self, coverage: Sequence[tuple[datetime, datetime]] = ()) -> None:
        self.coverage = list(coverage)
        self.calls: list[tuple[int, datetime, datetime]] = []

    async def tick_coverage(
        self,
        instrument_row_id: int,
        source: object,
        *,
        start: datetime,
        end: datetime,
        max_quiet: timedelta,
    ) -> tuple[tuple[datetime, datetime], ...]:
        del source, max_quiet
        self.calls.append((instrument_row_id, start, end))
        return tuple(self.coverage)


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, int, list[datetime]]] = []

    async def enqueue(self, source: str, instrument_row_id: int, hours: Iterable[datetime]) -> int:
        listed = list(hours)
        self.enqueued.append((source, instrument_row_id, listed))
        return len(listed)


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def append(self, **kwargs: Any) -> None:
        self.entries.append(kwargs)


def settings(**overrides: float) -> IngestSettings:
    values: dict[str, float | int] = {
        "registry_interval_seconds": 21600.0,
        "registry_deadline_seconds": 25200.0,
        "backfill_interval_seconds": 3600.0,
        "backfill_deadline_seconds": 5400.0,
        "backfill_window_seconds": 604800.0,
        "quote_deadline_seconds": 30.0,
        "backfill_fetch_timeout_seconds": 30.0,
        "backfill_concurrency": 3,
        "backfill_max_attempts_per_hour": 3,
        "backfill_backoff_seconds": 1.0,
        "backfill_stale_claim_seconds": 900.0,
        "recorder_batch_size": 500,
        "recorder_flush_interval_seconds": 5.0,
        "gap_interval_seconds": 900.0,
        "gap_deadline_seconds": 1800.0,
        "gap_window_seconds": 7200.0,
        "gap_settle_seconds": 120.0,
        "gap_max_quiet_seconds": 10.0,
        "gap_minimum_seconds": 60.0,
    }
    values.update(overrides)
    return IngestSettings(**values)


def universe(*, forex_has_history: bool = True) -> UniverseSettings:
    return UniverseSettings(
        instruments=(
            InstrumentRef(venue="bybit", venue_symbol="BTCUSDT", symbol="BTC/USDT"),
            InstrumentRef(
                venue="fxbroker",
                venue_symbol="EURUSD",
                symbol="EUR/USD",
                historical_source="dukascopy" if forex_has_history else None,
                historical_symbol="EURUSD" if forex_has_history else None,
            ),
        )
    )


def monitor(
    market_data: FakeMarketData,
    queue: FakeQueue,
    audit: FakeAudit,
    instruments: Mapping[InstrumentId, Instrument],
    *,
    forex_has_history: bool = True,
) -> GapMonitor:
    return GapMonitor(
        market_data=market_data,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        universe=universe(forex_has_history=forex_has_history),
        instruments=instruments,
        sources=dict.fromkeys(instruments, TickSource.BYBIT),
        audit=audit,  # type: ignore[arg-type]
        clock=FixedClock(NOW),
        settings=settings(),
    )


class TestARepairableGapIsQueued:
    async def test_missing_hours_are_enqueued_against_the_configured_feed(self) -> None:
        instrument = eurusd()
        market_data, queue, audit = FakeMarketData(), FakeQueue(), FakeAudit()
        gaps = monitor(market_data, queue, audit, {instrument.id: instrument})

        found = await gaps.run_once({instrument.id: 7})

        assert found
        assert queue.enqueued
        source, row_id, hours = queue.enqueued[0]
        assert source == "dukascopy"
        assert row_id == 7
        assert hours

    async def test_the_stats_separate_repairable_from_permanent(self) -> None:
        instrument = eurusd()
        gaps = monitor(FakeMarketData(), FakeQueue(), FakeAudit(), {instrument.id: instrument})

        await gaps.run_once({instrument.id: 7})

        assert gaps.stats.repairable_found > 0
        assert gaps.stats.permanent_found == 0
        assert gaps.stats.hours_queued > 0


class TestAPermanentGapIsRecordedNotQueued:
    async def test_a_venue_without_a_historical_feed_queues_nothing(self) -> None:
        """Bybit publishes no historical quote data, so there is nothing to fetch and
        queueing would create work that can never succeed."""
        instrument = btcusdt()
        market_data, queue, audit = FakeMarketData(), FakeQueue(), FakeAudit()
        gaps = monitor(market_data, queue, audit, {instrument.id: instrument})

        found = await gaps.run_once({instrument.id: 3})

        assert found
        assert queue.enqueued == []
        assert gaps.stats.permanent_found > 0
        assert gaps.stats.hours_queued == 0

    async def test_it_is_still_written_to_the_audit_log(self) -> None:
        """Detection that only logs is barely better than none. The audit entry is what
        survives a restart and what a backtest can be told about."""
        instrument = btcusdt()
        audit = FakeAudit()
        gaps = monitor(FakeMarketData(), FakeQueue(), audit, {instrument.id: instrument})

        await gaps.run_once({instrument.id: 3})

        assert audit.entries
        entry = audit.entries[0]
        assert entry["action"] == "gaps_detected"
        assert entry["payload"]["repairable"] is False
        assert entry["payload"]["gaps"]

    async def test_a_configured_instrument_that_loses_its_feed_becomes_permanent(self) -> None:
        """The distinction is configuration, not a venue name check."""
        instrument = eurusd()
        queue = FakeQueue()
        gaps = monitor(
            FakeMarketData(),
            queue,
            FakeAudit(),
            {instrument.id: instrument},
            forex_has_history=False,
        )

        await gaps.run_once({instrument.id: 7})

        assert queue.enqueued == []
        assert gaps.stats.permanent_found > 0


class TestTheWindow:
    async def test_the_trailing_edge_is_excluded(self) -> None:
        """The most recent seconds are indistinguishable from a quiet market, so
        including them would make every pass find a gap at its own edge."""
        instrument = btcusdt()
        market_data = FakeMarketData()
        gaps = monitor(market_data, FakeQueue(), FakeAudit(), {instrument.id: instrument})

        await gaps.run_once({instrument.id: 3})

        _, _, end = market_data.calls[0]
        assert end == NOW - timedelta(seconds=120)

    async def test_full_coverage_finds_nothing(self) -> None:
        instrument = btcusdt()
        covered = [(NOW - timedelta(hours=3), NOW)]
        market_data = FakeMarketData(covered)
        queue, audit = FakeQueue(), FakeAudit()
        gaps = monitor(market_data, queue, audit, {instrument.id: instrument})

        found = await gaps.run_once({instrument.id: 3})

        assert found == ()
        assert queue.enqueued == []
        assert audit.entries == []

    async def test_an_unknown_instrument_is_skipped_rather_than_raising(self) -> None:
        """A row id for something not in the universe is a configuration mismatch, not
        a reason to stop detecting gaps on everything else."""
        instrument = btcusdt()
        gaps = monitor(FakeMarketData(), FakeQueue(), FakeAudit(), {})

        assert await gaps.run_once({instrument.id: 3}) == ()


class TestHoursCovering:
    async def test_a_short_gap_still_needs_its_whole_hour(self) -> None:
        gap = Gap(
            start=datetime(2026, 8, 19, 10, 15, tzinfo=UTC),
            end=datetime(2026, 8, 19, 10, 20, tzinfo=UTC),
        )

        assert hours_covering(gap) == (datetime(2026, 8, 19, 10, 0, tzinfo=UTC),)

    async def test_a_straddling_gap_needs_both_hours(self) -> None:
        """Truncating to the start hour would leave the second half unqueued and
        therefore never repaired."""
        gap = Gap(
            start=datetime(2026, 8, 19, 10, 50, tzinfo=UTC),
            end=datetime(2026, 8, 19, 11, 10, tzinfo=UTC),
        )

        assert hours_covering(gap) == (
            datetime(2026, 8, 19, 10, 0, tzinfo=UTC),
            datetime(2026, 8, 19, 11, 0, tzinfo=UTC),
        )

    async def test_a_multi_hour_gap_lists_every_hour(self) -> None:
        gap = Gap(
            start=datetime(2026, 8, 19, 10, 30, tzinfo=UTC),
            end=datetime(2026, 8, 19, 13, 5, tzinfo=UTC),
        )

        assert len(hours_covering(gap)) == 4
