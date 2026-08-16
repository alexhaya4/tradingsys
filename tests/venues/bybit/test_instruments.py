"""Tests for the Bybit instrument mapping.

The fixtures in ``data/`` are unmodified responses from ``api.bybit.com``, recorded on
2026-08-16, one per category per pair, plus one tickers response for the funding
anchor. Hand written payloads would only prove that the parser agrees with my idea of
the venue, which is the belief most likely to be wrong.

The recorded values are asserted literally. When Bybit changes a minimum order size the
fixtures go stale and these tests fail, which is the point: the numbers move, they are
load bearing for position sizing, and a silent change is worse than a red suite.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tradingsys.core.currency import CurrencyRegistry, default_registry
from tradingsys.core.errors import UnknownCurrencyError
from tradingsys.core.financing import FinancingModel
from tradingsys.core.instrument import AssetClass, InstrumentStatus, QuantityUnit
from tradingsys.venues.bybit.instruments import (
    VENUE,
    instrument_from_linear,
    instrument_from_spot,
    next_funding_time,
)
from tradingsys.venues.errors import VenueResponseError

DATA = Path(__file__).parent / "data"
ANCHOR = datetime(2026, 8, 16, 16, tzinfo=UTC)


def recorded(name: str) -> dict[str, Any]:
    """One instrument object out of a recorded ``instruments-info`` response."""
    payload = json.loads((DATA / name).read_text())
    assert payload["retCode"] == 0, payload["retMsg"]
    entry: dict[str, Any] = payload["result"]["list"][0]
    return entry


@pytest.fixture
def currencies() -> CurrencyRegistry:
    return default_registry.copy()


class TestSpot:
    def test_btc_maps_to_the_recorded_values(self, currencies: CurrencyRegistry) -> None:
        instrument = instrument_from_spot(recorded("instruments_spot_BTCUSDT.json"), currencies)
        assert str(instrument.id) == f"{VENUE}:BTC/USDT"
        assert instrument.venue_symbol == "BTCUSDT"
        assert instrument.asset_class is AssetClass.CRYPTO_SPOT
        assert instrument.quantity_unit is QuantityUnit.UNITS
        assert instrument.contract_size == 1
        assert instrument.price_increment == Decimal("0.1")
        assert instrument.quantity_increment == Decimal("0.000001")
        assert instrument.min_quantity == Decimal("0.000001")
        assert instrument.max_quantity == Decimal("230")
        assert instrument.status is InstrumentStatus.ACTIVE

    def test_the_binding_minimum_on_spot_is_cash(self, currencies: CurrencyRegistry) -> None:
        # minOrderQty is 0.000001 BTC, worth about six cents. The floor that actually
        # rejects orders is minOrderAmt, five dollars, and it is denominated in the
        # quote asset rather than the base.
        instrument = instrument_from_spot(recorded("instruments_spot_BTCUSDT.json"), currencies)
        assert instrument.min_notional is not None
        assert instrument.min_notional.amount == Decimal("5")
        assert instrument.min_notional.currency.code == "USDT"

    def test_spot_has_no_financing(self, currencies: CurrencyRegistry) -> None:
        instrument = instrument_from_spot(recorded("instruments_spot_BTCUSDT.json"), currencies)
        assert instrument.financing.model is FinancingModel.NONE
        assert instrument.max_leverage is None

    def test_eth_has_its_own_precision(self, currencies: CurrencyRegistry) -> None:
        # The point of reading metadata per instrument: ETH ticks a hundred times
        # finer in price and ten times coarser in quantity than BTC.
        instrument = instrument_from_spot(recorded("instruments_spot_ETHUSDT.json"), currencies)
        assert instrument.price_increment == Decimal("0.01")
        assert instrument.quantity_increment == Decimal("0.00001")

    def test_the_price_precision_covers_the_tick(self, currencies: CurrencyRegistry) -> None:
        instrument = instrument_from_spot(recorded("instruments_spot_BTCUSDT.json"), currencies)
        assert instrument.price_precision == 1

    def test_bybit_never_closes(self, currencies: CurrencyRegistry) -> None:
        instrument = instrument_from_spot(recorded("instruments_spot_BTCUSDT.json"), currencies)
        assert instrument.is_open(datetime(2026, 8, 16, 3, 30, tzinfo=UTC))  # a Sunday
        assert instrument.schedule.always_open


class TestLinearPerpetual:
    def test_btc_maps_to_the_recorded_values(self, currencies: CurrencyRegistry) -> None:
        instrument = instrument_from_linear(
            recorded("instruments_linear_BTCUSDT.json"), currencies, funding_anchor=ANCHOR
        )
        assert str(instrument.id) == f"{VENUE}:BTC/USDT"
        assert instrument.asset_class is AssetClass.CRYPTO_PERPETUAL
        assert instrument.price_increment == Decimal("0.10")
        assert instrument.price_precision == 2
        assert instrument.quantity_increment == Decimal("0.001")
        assert instrument.min_quantity == Decimal("0.001")
        assert instrument.max_leverage == Decimal("100.00")
        assert instrument.settlement_currency.code == "USDT"

    def test_the_perpetual_minimum_is_a_thousand_times_the_spot_one(
        self, currencies: CurrencyRegistry
    ) -> None:
        # 0.001 BTC against 0.000001 BTC. Which category we trade therefore decides
        # whether a 200 dollar account can size a position at all, so the two are
        # separate instruments rather than one with a category flag.
        spot = instrument_from_spot(recorded("instruments_spot_BTCUSDT.json"), currencies)
        perpetual = instrument_from_linear(
            recorded("instruments_linear_BTCUSDT.json"), currencies, funding_anchor=ANCHOR
        )
        assert perpetual.min_quantity == spot.min_quantity * 1000

    def test_funding_uses_the_venues_interval_and_the_given_anchor(
        self, currencies: CurrencyRegistry
    ) -> None:
        instrument = instrument_from_linear(
            recorded("instruments_linear_BTCUSDT.json"), currencies, funding_anchor=ANCHOR
        )
        assert instrument.financing.model is FinancingModel.FUNDING_RATE
        assert instrument.financing.funding_interval == timedelta(hours=8)
        assert instrument.financing.funding_anchor == ANCHOR

    def test_funding_events_land_on_the_anchor_cycle(self, currencies: CurrencyRegistry) -> None:
        # The anchor is why it is required rather than derived: an eight hour interval
        # with the phase guessed wrong misprices every overnight hold by one payment.
        instrument = instrument_from_linear(
            recorded("instruments_linear_BTCUSDT.json"), currencies, funding_anchor=ANCHOR
        )
        events = instrument.financing.funding_events_between(
            datetime(2026, 8, 16, tzinfo=UTC), datetime(2026, 8, 17, tzinfo=UTC)
        )
        # Three a day at 00:00, 08:00, and 16:00 UTC, the phase the anchor puts them
        # on. The window is half open at the start, so the 00:00 settlement belongs to
        # the previous day's query and the next day's opens this one.
        assert [event.at for event in events] == [
            datetime(2026, 8, 16, 8, tzinfo=UTC),
            datetime(2026, 8, 16, 16, tzinfo=UTC),
            datetime(2026, 8, 17, 0, tzinfo=UTC),
        ]

    def test_a_dated_future_is_refused(self, currencies: CurrencyRegistry) -> None:
        # A quarterly expires and settles. Mapping it here would model it as a
        # perpetual that never expires, and the error surfaces at expiry.
        payload = recorded("instruments_linear_BTCUSDT.json") | {"contractType": "LinearFutures"}
        with pytest.raises(VenueResponseError, match="expected a LinearPerpetual"):
            instrument_from_linear(payload, currencies, funding_anchor=ANCHOR)

    def test_eth_has_its_own_step(self, currencies: CurrencyRegistry) -> None:
        instrument = instrument_from_linear(
            recorded("instruments_linear_ETHUSDT.json"), currencies, funding_anchor=ANCHOR
        )
        assert instrument.quantity_increment == Decimal("0.01")
        assert instrument.min_quantity == Decimal("0.01")
        assert instrument.price_increment == Decimal("0.01")


class TestNextFundingTime:
    def test_the_anchor_comes_from_the_tickers_response(self) -> None:
        payload = json.loads((DATA / "tickers_linear_BTCUSDT.json").read_text())
        entry = payload["result"]["list"][0]
        assert next_funding_time(entry) == datetime(2026, 8, 16, 16, tzinfo=UTC)

    def test_milliseconds_are_read_as_milliseconds(self) -> None:
        # Reading them as seconds puts the anchor in 1970 and every funding event with
        # it, without anything failing.
        assert next_funding_time(
            {"symbol": "BTCUSDT", "nextFundingTime": "1786896000000"}
        ) == datetime(2026, 8, 16, 16, tzinfo=UTC)

    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_a_non_instant_is_refused(self, raw: str) -> None:
        with pytest.raises(VenueResponseError, match="not an instant"):
            next_funding_time({"symbol": "BTCUSDT", "nextFundingTime": raw})

    def test_a_non_integer_is_refused(self) -> None:
        with pytest.raises(VenueResponseError, match="not an integer"):
            next_funding_time({"symbol": "BTCUSDT", "nextFundingTime": "soon"})

    def test_an_absent_field_is_refused(self) -> None:
        with pytest.raises(VenueResponseError, match="nextFundingTime is missing"):
            next_funding_time({"symbol": "BTCUSDT"})


class TestRefusingIncompletePayloads:
    """Every one of these would otherwise become a plausible default and a wrong size."""

    def test_a_missing_lot_size_filter_is_refused(self, currencies: CurrencyRegistry) -> None:
        payload = {
            k: v
            for k, v in recorded("instruments_spot_BTCUSDT.json").items()
            if k != "lotSizeFilter"
        }
        with pytest.raises(VenueResponseError, match="lotSizeFilter is missing"):
            instrument_from_spot(payload, currencies)

    def test_a_missing_tick_size_is_refused(self, currencies: CurrencyRegistry) -> None:
        payload = recorded("instruments_spot_BTCUSDT.json") | {"priceFilter": {}}
        with pytest.raises(VenueResponseError, match="tickSize is missing"):
            instrument_from_spot(payload, currencies)

    def test_an_empty_string_is_not_a_number(self, currencies: CurrencyRegistry) -> None:
        payload = recorded("instruments_spot_BTCUSDT.json")
        payload["lotSizeFilter"] = payload["lotSizeFilter"] | {"minOrderQty": ""}
        with pytest.raises(VenueResponseError, match="minOrderQty is missing or empty"):
            instrument_from_spot(payload, currencies)

    def test_a_numeric_json_type_is_still_refused(self, currencies: CurrencyRegistry) -> None:
        # Bybit sends every number as a string. A bare JSON number means the response
        # is not the shape this parser was written against, so it stops rather than
        # accepting a value that may have lost digits in the sender's encoder.
        payload = recorded("instruments_spot_BTCUSDT.json")
        payload["lotSizeFilter"] = payload["lotSizeFilter"] | {"minOrderQty": 0.000001}
        with pytest.raises(VenueResponseError, match="minOrderQty is missing or empty"):
            instrument_from_spot(payload, currencies)

    def test_an_unknown_status_is_refused(self, currencies: CurrencyRegistry) -> None:
        payload = recorded("instruments_spot_BTCUSDT.json") | {"status": "Paused"}
        with pytest.raises(VenueResponseError, match="unknown Bybit status"):
            instrument_from_spot(payload, currencies)

    @pytest.mark.parametrize("status", ["PreLaunch", "Delivering"])
    def test_a_not_yet_trading_instrument_is_halted(
        self, status: str, currencies: CurrencyRegistry
    ) -> None:
        payload = recorded("instruments_spot_BTCUSDT.json") | {"status": status}
        instrument = instrument_from_spot(payload, currencies)
        assert instrument.status is InstrumentStatus.HALTED
        assert not instrument.is_tradeable

    def test_a_closed_instrument_is_delisted(self, currencies: CurrencyRegistry) -> None:
        payload = recorded("instruments_spot_BTCUSDT.json") | {"status": "Closed"}
        assert instrument_from_spot(payload, currencies).status is InstrumentStatus.DELISTED

    def test_an_unknown_asset_is_refused(self, currencies: CurrencyRegistry) -> None:
        # Inventing a currency here would mean inventing its precision, which decides
        # how every amount in it is rounded.
        payload = recorded("instruments_spot_BTCUSDT.json") | {"baseCoin": "NOTACOIN"}
        with pytest.raises(UnknownCurrencyError):
            instrument_from_spot(payload, currencies)

    def test_a_missing_funding_interval_is_refused(self, currencies: CurrencyRegistry) -> None:
        payload = {
            k: v
            for k, v in recorded("instruments_linear_BTCUSDT.json").items()
            if k != "fundingInterval"
        }
        with pytest.raises(VenueResponseError, match="fundingInterval is missing"):
            instrument_from_linear(payload, currencies, funding_anchor=ANCHOR)

    def test_a_non_numeric_funding_interval_is_refused(self, currencies: CurrencyRegistry) -> None:
        payload = recorded("instruments_linear_BTCUSDT.json") | {"fundingInterval": "eight hours"}
        with pytest.raises(VenueResponseError, match="fundingInterval is not an integer"):
            instrument_from_linear(payload, currencies, funding_anchor=ANCHOR)

    def test_a_missing_leverage_filter_is_refused(self, currencies: CurrencyRegistry) -> None:
        payload = {
            k: v
            for k, v in recorded("instruments_linear_BTCUSDT.json").items()
            if k != "leverageFilter"
        }
        with pytest.raises(VenueResponseError, match="leverageFilter is missing"):
            instrument_from_linear(payload, currencies, funding_anchor=ANCHOR)
