"""Integration tests against a real PostgreSQL with TimescaleDB.

These prove the things that only a real database can: that the schema matches the
repositories, that the hypertables exist, that concurrent audit appends serialise into
one chain, and that the append-only trigger actually fires.

Requires the compose stack. See conftest.py for how to run them.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import asyncpg
import pytest

from tests.factories import btcusdt, eurusd, eurusd_lots, usdjpy
from tradingsys.core.errors import PersistenceError
from tradingsys.persistence.audit import GENESIS_HASH, AuditCategory
from tradingsys.venues.enums import CandlePrice, Granularity
from tradingsys.venues.models import Candle, Quote

if TYPE_CHECKING:
    from tradingsys.persistence.audit import AuditLog
    from tradingsys.persistence.database import Database
    from tradingsys.persistence.repositories import InstrumentRepository, MarketDataRepository

pytestmark = pytest.mark.integration

NOW = datetime(2025, 3, 5, 12, 0, tzinfo=UTC)


class TestDatabase:
    async def test_health_check(self, database: Database) -> None:
        await database.check()

    async def test_timescale_is_installed(self, database: Database) -> None:
        version = await database.timescale_version()
        assert version is not None, "the schema depends on TimescaleDB hypertables"

    async def test_bars_and_ticks_are_hypertables(self, database: Database) -> None:
        rows = await database.fetch(
            "SELECT hypertable_name FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema = 'public'"
        )
        names = {row["hypertable_name"] for row in rows}
        assert {"ohlcv_bars", "ticks"} <= names

    async def test_numerics_come_back_as_decimal(self, database: Database) -> None:
        value = await database.fetchval("SELECT 1.08500::numeric")
        assert isinstance(value, Decimal)
        assert value == Decimal("1.08500")

    async def test_a_float_is_refused_for_a_numeric_column(self, database: Database) -> None:
        # asyncpg wraps a codec failure in DataError, keeping our message as the cause,
        # so the operator still sees why the value was refused.
        with pytest.raises(asyncpg.DataError, match="refusing to store the float"):
            await database.fetchval("SELECT $1::numeric", 1.085)

    async def test_a_decimal_is_accepted_for_a_numeric_column(self, database: Database) -> None:
        assert await database.fetchval("SELECT $1::numeric", Decimal("1.085")) == Decimal("1.085")

    async def test_pool_stats_reflect_the_configuration(self, database: Database) -> None:
        stats = database.pool_stats()
        assert stats["max_size"] == database.settings.max_pool_size
        assert stats["size"] >= database.settings.min_pool_size

    async def test_the_pool_refuses_queries_before_connecting(self, database: Database) -> None:
        await database.close()
        with pytest.raises(PersistenceError, match="not open"):
            await database.fetchval("SELECT 1")

    async def test_close_is_idempotent(self, database: Database) -> None:
        await database.close()
        await database.close()

    async def test_a_failed_transaction_rolls_back(
        self, database: Database, instruments: InstrumentRepository
    ) -> None:
        async def insert_then_fail() -> None:
            async with database.transaction() as connection:
                await connection.fetchval(
                    "INSERT INTO instruments (venue, symbol, venue_symbol, asset_class, "
                    "base_currency, quote_currency, settlement_currency, price_increment, "
                    "price_precision, quantity_unit, contract_size, quantity_increment, "
                    "min_quantity, financing, schedule, status) VALUES "
                    "('v','S','S','fx_spot','EUR','USD','USD',1,1,'units',1,1,1,"
                    "'{}'::jsonb,'{}'::jsonb,'active') RETURNING id"
                )
                raise RuntimeError("deliberate failure")

        with pytest.raises(RuntimeError):
            await insert_then_fail()
        assert await instruments.list_for_venue("v") == ()


class TestInstrumentRepository:
    async def test_round_trip(self, instruments: InstrumentRepository) -> None:
        original = eurusd()
        await instruments.upsert(original)
        restored = await instruments.get(original.id)
        assert restored == original

    async def test_round_trip_preserves_the_venue_symbol_mapping(
        self, instruments: InstrumentRepository
    ) -> None:
        await instruments.upsert(eurusd())
        restored = await instruments.get(eurusd().id)
        assert restored is not None
        assert restored.venue_symbol == "EUR_USD"
        assert restored.symbol == "EUR/USD"

    async def test_round_trip_preserves_decimal_precision(
        self, instruments: InstrumentRepository
    ) -> None:
        await instruments.upsert(btcusdt())
        restored = await instruments.get(btcusdt().id)
        assert restored is not None
        assert restored.quantity_increment == Decimal("0.00001")
        assert restored.min_notional == btcusdt().min_notional

    async def test_round_trip_preserves_the_schedule_and_financing(
        self, instruments: InstrumentRepository
    ) -> None:
        await instruments.upsert(eurusd())
        restored = await instruments.get(eurusd().id)
        assert restored is not None
        assert restored.schedule == eurusd().schedule
        assert restored.financing == eurusd().financing
        assert restored.schedule.is_open(NOW)

    async def test_upsert_replaces_a_changed_definition(
        self, instruments: InstrumentRepository
    ) -> None:
        await instruments.upsert(eurusd())
        widened = replace(eurusd(), max_leverage=Decimal(20))
        await instruments.upsert(widened)
        restored = await instruments.get(eurusd().id)
        assert restored is not None
        assert restored.max_leverage == Decimal(20)

    async def test_the_same_symbol_at_two_venues_coexists(
        self, instruments: InstrumentRepository
    ) -> None:
        await instruments.upsert(eurusd())
        await instruments.upsert(eurusd_lots())
        assert (await instruments.get(eurusd().id)) == eurusd()
        assert (await instruments.get(eurusd_lots().id)) == eurusd_lots()

    async def test_missing_instruments_return_none(self, instruments: InstrumentRepository) -> None:
        assert await instruments.get(eurusd().id) is None

    async def test_row_id_requires_the_instrument_to_exist(
        self, instruments: InstrumentRepository
    ) -> None:
        with pytest.raises(PersistenceError, match="store its definition"):
            await instruments.row_id(eurusd().id)

    async def test_upsert_many_is_atomic(self, instruments: InstrumentRepository) -> None:
        written = await instruments.upsert_many([eurusd(), eurusd_lots(), btcusdt()])
        assert written == 3
        assert len(await instruments.list_for_venue("fxbroker")) == 1
        assert len(await instruments.list_for_venue("cryptoex")) == 1

    async def test_list_is_ordered_by_symbol(self, instruments: InstrumentRepository) -> None:
        await instruments.upsert_many([usdjpy(), eurusd()])
        listed = await instruments.list_for_venue("fxbroker")
        assert [item.symbol for item in listed] == ["EUR/USD", "USD/JPY"]

    async def test_the_base_is_not_quote_constraint_is_enforced(self, database: Database) -> None:
        with pytest.raises(asyncpg.CheckViolationError):
            await database.execute(
                "INSERT INTO instruments (venue, symbol, venue_symbol, asset_class, "
                "base_currency, quote_currency, settlement_currency, price_increment, "
                "price_precision, quantity_unit, contract_size, quantity_increment, "
                "min_quantity, financing, schedule, status) VALUES "
                "('v','S','S','fx_spot','USD','USD','USD',1,1,'units',1,1,1,"
                "'{}'::jsonb,'{}'::jsonb,'active')"
            )


class TestMarketDataRepository:
    async def _prepare(self, instruments: InstrumentRepository) -> int:
        return await instruments.upsert(eurusd())

    def _bar(self, hour: int, *, complete: bool = True, close: str = "1.0855") -> Candle:
        return Candle(
            instrument_id=eurusd().id,
            granularity=Granularity.M5,
            price=CandlePrice.MID,
            start=datetime(2025, 3, 5, hour, 0, tzinfo=UTC),
            open=Decimal("1.0850"),
            high=Decimal("1.0870"),
            low=Decimal("1.0840"),
            close=Decimal(close),
            volume=Decimal("1000"),
            trade_count=10,
            complete=complete,
        )

    async def test_bar_round_trip(
        self, instruments: InstrumentRepository, market_data: MarketDataRepository
    ) -> None:
        row_id = await self._prepare(instruments)
        await market_data.store_bars(row_id, [self._bar(10), self._bar(11)])
        stored = await market_data.fetch_bars(row_id, Granularity.M5, CandlePrice.MID)
        assert [bar.start.hour for bar in stored] == [10, 11]
        assert stored[0].open == Decimal("1.0850")
        assert stored[0].instrument_id == eurusd().id

    async def test_bars_are_returned_in_ascending_time_order(
        self, instruments: InstrumentRepository, market_data: MarketDataRepository
    ) -> None:
        row_id = await self._prepare(instruments)
        await market_data.store_bars(row_id, [self._bar(12), self._bar(10), self._bar(11)])
        stored = await market_data.fetch_bars(row_id, Granularity.M5, CandlePrice.MID)
        assert [bar.start.hour for bar in stored] == [10, 11, 12]

    async def test_the_fetch_range_is_half_open(
        self, instruments: InstrumentRepository, market_data: MarketDataRepository
    ) -> None:
        row_id = await self._prepare(instruments)
        await market_data.store_bars(row_id, [self._bar(hour) for hour in (10, 11, 12)])
        stored = await market_data.fetch_bars(
            row_id,
            Granularity.M5,
            CandlePrice.MID,
            start=datetime(2025, 3, 5, 10, 0, tzinfo=UTC),
            end=datetime(2025, 3, 5, 12, 0, tzinfo=UTC),
        )
        assert [bar.start.hour for bar in stored] == [10, 11]

    async def test_reingesting_is_idempotent(
        self, instruments: InstrumentRepository, market_data: MarketDataRepository
    ) -> None:
        row_id = await self._prepare(instruments)
        await market_data.store_bars(row_id, [self._bar(10)])
        await market_data.store_bars(row_id, [self._bar(10)])
        stored = await market_data.fetch_bars(row_id, Granularity.M5, CandlePrice.MID)
        assert len(stored) == 1

    async def test_an_incomplete_bar_is_updated_when_it_closes(
        self, instruments: InstrumentRepository, market_data: MarketDataRepository
    ) -> None:
        row_id = await self._prepare(instruments)
        await market_data.store_bars(row_id, [self._bar(10, complete=False, close="1.0851")])
        await market_data.store_bars(row_id, [self._bar(10, complete=True, close="1.0859")])
        stored = await market_data.fetch_bars(row_id, Granularity.M5, CandlePrice.MID)
        assert stored[0].close == Decimal("1.0859")
        assert stored[0].complete

    async def test_a_closed_bar_is_never_rewritten(
        self, instruments: InstrumentRepository, market_data: MarketDataRepository
    ) -> None:
        row_id = await self._prepare(instruments)
        await market_data.store_bars(row_id, [self._bar(10, close="1.0855")])
        await market_data.store_bars(row_id, [self._bar(10, close="1.0869")])
        stored = await market_data.fetch_bars(row_id, Granularity.M5, CandlePrice.MID)
        assert stored[0].close == Decimal("1.0855")

    async def test_price_components_are_stored_separately(
        self, instruments: InstrumentRepository, market_data: MarketDataRepository
    ) -> None:
        row_id = await self._prepare(instruments)
        mid = self._bar(10)
        await market_data.store_bars(row_id, [mid, replace(mid, price=CandlePrice.BID)])
        assert len(await market_data.fetch_bars(row_id, Granularity.M5, CandlePrice.MID)) == 1
        assert len(await market_data.fetch_bars(row_id, Granularity.M5, CandlePrice.BID)) == 1

    async def test_latest_bar_start_is_the_backfill_resume_point(
        self, instruments: InstrumentRepository, market_data: MarketDataRepository
    ) -> None:
        row_id = await self._prepare(instruments)
        assert await market_data.latest_bar_start(row_id, Granularity.M5, CandlePrice.MID) is None
        await market_data.store_bars(row_id, [self._bar(10), self._bar(12)])
        latest = await market_data.latest_bar_start(row_id, Granularity.M5, CandlePrice.MID)
        assert latest == datetime(2025, 3, 5, 12, 0, tzinfo=UTC)

    async def test_storing_nothing_is_allowed(self, market_data: MarketDataRepository) -> None:
        assert await market_data.store_bars(1, []) == 0
        assert await market_data.store_ticks(1, []) == 0

    async def test_an_impossible_bar_is_rejected_by_the_database(
        self, instruments: InstrumentRepository, database: Database
    ) -> None:
        row_id = await self._prepare(instruments)
        with pytest.raises(asyncpg.CheckViolationError):
            await database.execute(
                "INSERT INTO ohlcv_bars (instrument_id, granularity, price_component, ts, "
                "open, high, low, close) VALUES ($1, '5m', 'mid', $2, 1.09, 1.08, 1.07, 1.085)",
                row_id,
                NOW,
            )

    async def test_tick_round_trip(
        self, instruments: InstrumentRepository, market_data: MarketDataRepository
    ) -> None:
        row_id = await self._prepare(instruments)
        ticks = [
            Quote(
                instrument_id=eurusd().id,
                ts=NOW + timedelta(milliseconds=index),
                bid=Decimal("1.08500"),
                ask=Decimal("1.08512"),
                bid_size=Decimal("1000000"),
            )
            for index in range(3)
        ]
        assert await market_data.store_ticks(row_id, ticks) == 3
        stored = await market_data.fetch_ticks(row_id)
        assert len(stored) == 3
        assert stored[0].bid == Decimal("1.08500")
        assert stored[0].bid_size == Decimal("1000000")
        assert stored[0].ask_size is None

    async def test_duplicate_ticks_keep_the_first_observation(
        self, instruments: InstrumentRepository, market_data: MarketDataRepository
    ) -> None:
        row_id = await self._prepare(instruments)
        first = Quote(eurusd().id, NOW, Decimal("1.08500"), Decimal("1.08512"))
        second = Quote(eurusd().id, NOW, Decimal("1.09000"), Decimal("1.09012"))
        await market_data.store_ticks(row_id, [first])
        await market_data.store_ticks(row_id, [second])
        stored = await market_data.fetch_ticks(row_id)
        assert len(stored) == 1
        assert stored[0].bid == Decimal("1.08500")

    async def test_a_crossed_tick_is_rejected_by_the_database(
        self, instruments: InstrumentRepository, database: Database
    ) -> None:
        row_id = await self._prepare(instruments)
        with pytest.raises(asyncpg.CheckViolationError):
            await database.execute(
                "INSERT INTO ticks (instrument_id, ts, bid, ask) VALUES ($1, $2, 1.09, 1.08)",
                row_id,
                NOW,
            )

    async def test_bars_require_a_known_instrument(self, database: Database) -> None:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await database.execute(
                "INSERT INTO ohlcv_bars (instrument_id, granularity, price_component, ts, "
                "open, high, low, close) VALUES (999999, '5m', 'mid', $1, 1, 1, 1, 1)",
                NOW,
            )


class TestAuditLogStorage:
    async def _append(self, audit_log: AuditLog, index: int) -> None:
        await audit_log.append(
            correlation_id=f"corr-{index}",
            category=AuditCategory.RISK,
            actor="risk.position_sizer",
            action="rejected_order",
            summary=f"decision {index}",
            payload={"index": index, "size": Decimal("1.5")},
            instrument_id=eurusd().id,
        )

    async def test_appending_assigns_a_contiguous_sequence(self, audit_log: AuditLog) -> None:
        for index in range(5):
            await self._append(audit_log, index)
        entries = await audit_log.read()
        assert [entry.sequence for entry in entries] == [1, 2, 3, 4, 5]

    async def test_the_first_entry_links_to_genesis(self, audit_log: AuditLog) -> None:
        await self._append(audit_log, 0)
        entries = await audit_log.read()
        assert entries[0].previous_hash == GENESIS_HASH

    async def test_each_entry_links_to_its_predecessor(self, audit_log: AuditLog) -> None:
        for index in range(3):
            await self._append(audit_log, index)
        entries = await audit_log.read()
        assert entries[1].links_to(entries[0])
        assert entries[2].links_to(entries[1])

    async def test_the_stored_chain_verifies(self, audit_log: AuditLog) -> None:
        for index in range(10):
            await self._append(audit_log, index)
        report = await audit_log.verify()
        assert report.is_intact
        assert report.entries_checked == 10

    async def test_payload_survives_the_round_trip(self, audit_log: AuditLog) -> None:
        await audit_log.append(
            correlation_id="corr-1",
            category=AuditCategory.ORDER,
            actor="execution.router",
            action="submitted_order",
            summary="submitted a market order",
            payload={"quantity": "10000", "nested": {"price": "1.08500"}},
        )
        entries = await audit_log.read()
        assert entries[0].payload == {"quantity": "10000", "nested": {"price": "1.08500"}}

    async def test_reading_a_tampered_row_raises(
        self, audit_log: AuditLog, database: Database
    ) -> None:
        # The trigger blocks UPDATE, so tampering is simulated the only way it could
        # really happen: by disabling the trigger, as a database superuser could.
        await self._append(audit_log, 0)
        async with database.transaction() as connection:
            await connection.execute("ALTER TABLE audit_log DISABLE TRIGGER USER")
            await connection.execute("UPDATE audit_log SET summary = 'rewritten'")
            await connection.execute("ALTER TABLE audit_log ENABLE TRIGGER USER")
        with pytest.raises(PersistenceError, match="has been altered"):
            await audit_log.read()

    async def test_updates_are_rejected_by_the_database(
        self, audit_log: AuditLog, database: Database
    ) -> None:
        await self._append(audit_log, 0)
        with pytest.raises(asyncpg.PostgresError, match="append only"):
            await database.execute("UPDATE audit_log SET summary = 'rewritten'")

    async def test_deletes_are_rejected_by_the_database(
        self, audit_log: AuditLog, database: Database
    ) -> None:
        await self._append(audit_log, 0)
        with pytest.raises(asyncpg.PostgresError, match="append only"):
            await database.execute("DELETE FROM audit_log")
        assert await audit_log.count() == 1

    async def test_two_entries_cannot_claim_the_same_predecessor(
        self, audit_log: AuditLog, database: Database
    ) -> None:
        await self._append(audit_log, 0)
        head = await audit_log.head()
        assert head is not None
        with pytest.raises(asyncpg.UniqueViolationError):
            await database.execute(
                "INSERT INTO audit_log (sequence, ts, correlation_id, category, actor, action, "
                "summary, payload, previous_hash, entry_hash) "
                "VALUES (99, now(), 'c', 'system', 'a', 'b', 's', '{}'::jsonb, $1, $2)",
                head.previous_hash,
                "f" * 64,
            )

    async def test_concurrent_appends_produce_one_unbroken_chain(self, audit_log: AuditLog) -> None:
        # The advisory lock is what makes this hold. Without it, two writers could read
        # the same head and both claim to follow it.
        await asyncio.gather(*(self._append(audit_log, index) for index in range(20)))
        entries = await audit_log.read()
        assert [entry.sequence for entry in entries] == list(range(1, 21))
        report = await audit_log.verify()
        assert report.is_intact

    async def test_filtering_by_correlation_id(self, audit_log: AuditLog) -> None:
        for index in range(3):
            await self._append(audit_log, index)
        found = await audit_log.read(correlation_id="corr-1")
        assert [entry.summary for entry in found] == ["decision 1"]

    async def test_filtering_by_category(self, audit_log: AuditLog) -> None:
        await self._append(audit_log, 0)
        await audit_log.append(
            correlation_id="corr-x",
            category=AuditCategory.SYSTEM,
            actor="app",
            action="started",
            summary="process started",
        )
        found = await audit_log.read(category=AuditCategory.SYSTEM)
        assert len(found) == 1
        assert found[0].action == "started"

    async def test_filtering_by_sequence_range_is_inclusive(self, audit_log: AuditLog) -> None:
        for index in range(5):
            await self._append(audit_log, index)
        found = await audit_log.read(start_sequence=2, end_sequence=4)
        assert [entry.sequence for entry in found] == [2, 3, 4]

    async def test_limit(self, audit_log: AuditLog) -> None:
        for index in range(5):
            await self._append(audit_log, index)
        assert len(await audit_log.read(limit=2)) == 2

    async def test_head_of_an_empty_log_is_none(self, audit_log: AuditLog) -> None:
        assert await audit_log.head() is None
        assert await audit_log.count() == 0
        assert (await audit_log.verify()).is_intact

    async def test_head_is_the_latest_entry(self, audit_log: AuditLog) -> None:
        for index in range(3):
            await self._append(audit_log, index)
        head = await audit_log.head()
        assert head is not None
        assert head.sequence == 3

    async def test_timestamps_are_returned_in_utc(self, audit_log: AuditLog) -> None:
        await self._append(audit_log, 0)
        entries = await audit_log.read()
        assert entries[0].ts.utcoffset() == timedelta(0)
