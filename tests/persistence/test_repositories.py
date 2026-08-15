"""Unit tests for query construction and row mapping.

Statement text and parameter numbering are asserted here so a mistake shows up as a
failing unit test rather than as a runtime error against a live database. Round trips
through PostgreSQL are covered in test_integration.py.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tests.factories import btcusdt, eurusd, eurusd_lots
from tradingsys.core.errors import PersistenceError
from tradingsys.core.instrument import InstrumentId
from tradingsys.core.provenance import TickSource
from tradingsys.persistence.repositories import (
    INSTRUMENT_ARG_ORDER,
    UPSERT_BAR,
    UPSERT_INSTRUMENT,
    build_bar_query,
    build_tick_query,
    candle_from_row,
    instrument_args,
    quote_from_row,
)
from tradingsys.venues.enums import CandlePrice, Granularity

NOW = datetime(2025, 3, 5, 12, 0, tzinfo=UTC)


class FakeRecord(dict[str, Any]):
    """A stand-in for asyncpg.Record, which is a mapping for our purposes."""


class TestInstrumentArguments:
    def test_argument_count_matches_the_statement_placeholders(self) -> None:
        placeholders = {
            token
            for token in UPSERT_INSTRUMENT.replace(",", " ").replace(")", " ").split()
            if token.startswith("$")
        }
        assert len(placeholders) == len(INSTRUMENT_ARG_ORDER)

    def test_arguments_are_in_placeholder_order(self) -> None:
        args = instrument_args(eurusd())
        assert args[0] == "fxbroker"
        assert args[1] == "EUR/USD"
        assert args[INSTRUMENT_ARG_ORDER.index("price_increment")] == "0.00001"

    def test_decimals_are_passed_as_strings_not_floats(self) -> None:
        for value in instrument_args(eurusd()):
            assert not isinstance(value, float)

    def test_optional_fields_are_none_rather_than_missing(self) -> None:
        args = instrument_args(btcusdt())
        assert args[INSTRUMENT_ARG_ORDER.index("pip_size")] is None
        assert args[INSTRUMENT_ARG_ORDER.index("max_leverage")] is None

    def test_lot_sized_instruments_carry_their_contract_size(self) -> None:
        args = instrument_args(eurusd_lots())
        assert args[INSTRUMENT_ARG_ORDER.index("contract_size")] == "100000"
        assert args[INSTRUMENT_ARG_ORDER.index("quantity_unit")] == "lots"


class TestBarStatement:
    def test_the_two_volume_columns_are_written_separately(self) -> None:
        # One column holding both meanings is what this replaced. The word boundary
        # matters: traded_volume contains volume as a substring.
        assert "tick_count" in UPSERT_BAR
        assert "traded_volume" in UPSERT_BAR
        assert not re.search(r"\bvolume\b", UPSERT_BAR)
        assert not re.search(r"\btrade_count\b", UPSERT_BAR)

    def test_a_closed_bar_is_never_overwritten(self) -> None:
        # The guard is what stops a late correction from rewriting a bar that a
        # decision was already made on.
        assert "WHERE ohlcv_bars.complete = false" in UPSERT_BAR

    def test_conflict_target_matches_the_primary_key(self) -> None:
        assert "ON CONFLICT (instrument_id, granularity, price_component, ts)" in UPSERT_BAR


class TestBarQuery:
    def test_minimal_query(self) -> None:
        query, args = build_bar_query(7, Granularity.M5, CandlePrice.MID)
        assert args == [7, "5m", "mid"]
        assert "instrument_id = $1" in query
        assert "granularity = $2" in query
        assert "price_component = $3" in query
        assert query.endswith("ORDER BY ts ASC")

    def test_the_range_is_half_open(self) -> None:
        query, args = build_bar_query(
            7,
            Granularity.M5,
            CandlePrice.MID,
            start=NOW,
            end=datetime(2025, 3, 6, tzinfo=UTC),
        )
        assert "ts >= $4" in query
        assert "ts < $5" in query
        assert args[3] == NOW

    def test_only_the_end_bound(self) -> None:
        query, args = build_bar_query(7, Granularity.M5, CandlePrice.MID, end=NOW)
        assert "ts < $4" in query
        assert len(args) == 4

    def test_limit_is_numbered_last(self) -> None:
        query, args = build_bar_query(7, Granularity.M5, CandlePrice.MID, start=NOW, limit=100)
        assert query.endswith("LIMIT $5")
        assert args[-1] == 100

    def test_a_non_positive_limit_is_rejected(self) -> None:
        with pytest.raises(PersistenceError, match="limit must be at least 1"):
            build_bar_query(7, Granularity.M5, CandlePrice.MID, limit=0)

    def test_the_query_reads_the_joined_view(self) -> None:
        query, _ = build_bar_query(7, Granularity.M5, CandlePrice.MID)
        assert "ohlcv_bars_with_instrument" in query


class TestTickQuery:
    def test_minimal_query(self) -> None:
        query, args = build_tick_query(7, TickSource.CTRADER)
        assert args == [7, "ctrader"]
        assert "source = $2" in query
        assert query.endswith("ORDER BY ts ASC")

    def test_bounds_and_limit(self) -> None:
        query, args = build_tick_query(7, TickSource.CTRADER, start=NOW, end=NOW, limit=10)
        assert "ts >= $3" in query
        assert "ts < $4" in query
        assert query.endswith("LIMIT $5")
        assert args == [7, "ctrader", NOW, NOW, 10]

    def test_a_non_positive_limit_is_rejected(self) -> None:
        with pytest.raises(PersistenceError, match="limit must be at least 1"):
            build_tick_query(7, TickSource.CTRADER, limit=-5)

    def test_an_ordinary_read_uses_the_full_tick_view(self) -> None:
        query, _ = build_tick_query(7, TickSource.DUKASCOPY)
        assert "ticks_with_instrument" in query

    def test_a_calibration_read_uses_the_execution_venue_view(self) -> None:
        # Two barriers: the view excludes research rows in the database, and the return
        # type refuses them in Python.
        query, _ = build_tick_query(7, TickSource.CTRADER, calibration=True)
        assert "execution_venue_ticks" in query
        assert "ticks_with_instrument" not in query


class TestRowMapping:
    def test_candle_from_row(self) -> None:
        bar = candle_from_row(
            FakeRecord(
                instrument_id=7,
                venue="fxbroker",
                symbol="EUR/USD",
                granularity="5m",
                price_component="mid",
                ts=NOW,
                open=Decimal("1.0850"),
                high=Decimal("1.0860"),
                low=Decimal("1.0845"),
                close=Decimal("1.0855"),
                tick_count=42,
                traded_volume=Decimal("1234"),
                complete=True,
            )
        )
        assert bar.instrument_id == InstrumentId("fxbroker", "EUR/USD")
        assert bar.granularity is Granularity.M5
        assert bar.price is CandlePrice.MID
        assert bar.close == Decimal("1.0855")
        assert bar.tick_count == 42
        assert bar.traded_volume == Decimal("1234")

    def test_candle_mapping_rejects_an_impossible_bar(self) -> None:
        # A row that violates the OHLC relationship must not become a Candle, even
        # though a check constraint should have stopped it being stored.
        with pytest.raises(Exception, match="high"):
            candle_from_row(
                FakeRecord(
                    instrument_id=7,
                    venue="fxbroker",
                    symbol="EUR/USD",
                    granularity="5m",
                    price_component="mid",
                    ts=NOW,
                    open=Decimal("1.0850"),
                    high=Decimal("1.0800"),
                    low=Decimal("1.0845"),
                    close=Decimal("1.0855"),
                    tick_count=0,
                    traded_volume=None,
                    complete=True,
                )
            )

    def test_quote_from_row(self) -> None:
        tick = quote_from_row(
            FakeRecord(
                instrument_id=7,
                venue="fxbroker",
                symbol="EUR/USD",
                source="ctrader",
                ts=NOW,
                bid=Decimal("1.08500"),
                ask=Decimal("1.08512"),
                bid_size=Decimal("1000000"),
                ask_size=None,
                tradeable=True,
            )
        )
        assert tick.instrument_id == InstrumentId("fxbroker", "EUR/USD")
        assert tick.spread == Decimal("0.00012")
        assert tick.ask_size is None

    def test_quote_mapping_rejects_a_crossed_row(self) -> None:
        with pytest.raises(Exception, match="crossed"):
            quote_from_row(
                FakeRecord(
                    instrument_id=7,
                    venue="fxbroker",
                    symbol="EUR/USD",
                    source="ctrader",
                    ts=NOW,
                    bid=Decimal("1.09"),
                    ask=Decimal("1.08"),
                    bid_size=None,
                    ask_size=None,
                    tradeable=True,
                )
            )
