"""Contract tests for the venue abstract base classes.

These assert properties of the interfaces themselves: that they are abstract, that a
conforming implementation satisfies them, that the shared lifecycle works, and that
the vocabulary stays venue-neutral.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.factories import USD, btcusdt, eurusd
from tests.venues.conforming import (
    ConformingExecution,
    ConformingMarketData,
    PartialMarketData,
)
from tradingsys.core.money import Money
from tradingsys.venues import base as base_module
from tradingsys.venues import enums as enums_module
from tradingsys.venues import models as models_module
from tradingsys.venues.base import ExecutionVenue, MarketDataSource, VenueConnection
from tradingsys.venues.enums import (
    CandlePrice,
    Granularity,
    OrderSide,
    OrderType,
    PositionMode,
    TimeInForce,
)
from tradingsys.venues.errors import (
    InstrumentNotFoundError,
    OrderNotFoundError,
    UnsupportedVenueOperationError,
    VenueError,
)
from tradingsys.venues.models import (
    AccountSnapshot,
    Candle,
    OrderRequest,
    Quote,
    VenueCapabilities,
)

NOW = datetime(2025, 3, 5, 12, 0, tzinfo=UTC)

CAPABILITIES = VenueCapabilities(
    venue="fxbroker",
    position_mode=PositionMode.NETTING,
    order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}),
    time_in_force=frozenset({TimeInForce.GTC, TimeInForce.FOK}),
    granularities=frozenset({Granularity.M5, Granularity.H1}),
    candle_prices=frozenset({CandlePrice.BID, CandlePrice.ASK, CandlePrice.MID}),
    max_candles_per_request=500,
    supports_quote_stream=True,
    supports_historical_candles=True,
)


def market_data(**kwargs: object) -> ConformingMarketData:
    return ConformingMarketData("fxbroker", CAPABILITIES, **kwargs)  # type: ignore[arg-type]


class TestAbstractness:
    def test_the_interfaces_cannot_be_instantiated(self) -> None:
        for cls in (VenueConnection, MarketDataSource, ExecutionVenue):
            with pytest.raises(TypeError, match="abstract"):
                cls()  # type: ignore[abstract]

    def test_a_partial_implementation_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            PartialMarketData()  # type: ignore[abstract]

    def test_the_error_names_a_missing_method(self) -> None:
        with pytest.raises(TypeError) as caught:
            PartialMarketData()  # type: ignore[abstract]
        assert "fetch_instruments" in str(caught.value)

    @pytest.mark.parametrize("cls", [MarketDataSource, ExecutionVenue])
    def test_every_declared_method_is_abstract(self, cls: type[VenueConnection]) -> None:
        # A method that is public and not abstract would be inherited behaviour, which
        # these interfaces deliberately do not provide except for the context manager.
        allowed_concrete = {"__aenter__", "__aexit__"}
        for name, member in inspect.getmembers(cls):
            if name.startswith("_") and name not in allowed_concrete:
                continue
            if name in allowed_concrete:
                continue
            if callable(member) or isinstance(member, property):
                assert name in cls.__abstractmethods__, f"{cls.__name__}.{name} is not abstract"

    def test_market_data_cannot_place_orders(self) -> None:
        # The read/write split is the point: a research process holding a
        # MarketDataSource has no order entry surface at all.
        assert not hasattr(MarketDataSource, "place_order")
        assert not hasattr(MarketDataSource, "close_position")


class TestLifecycle:
    async def test_context_manager_connects_and_closes(self) -> None:
        source = market_data()
        async with source as entered:
            assert entered is source
            assert source.connect_calls == 1
            assert source.close_calls == 0
        assert source.close_calls == 1

    async def test_close_runs_even_when_the_body_raises(self) -> None:
        source = market_data()
        with pytest.raises(RuntimeError):
            async with source:
                raise RuntimeError("boom")
        assert source.close_calls == 1

    async def test_ping_returns_a_duration(self) -> None:
        assert await market_data().ping() >= 0

    def test_capabilities_name_the_same_venue(self) -> None:
        source = market_data()
        assert source.capabilities.venue == source.venue


class TestMarketDataContract:
    async def test_instruments_round_trip(self) -> None:
        source = market_data(instruments=[eurusd(), btcusdt()])
        assert len(await source.fetch_instruments()) == 2
        assert (await source.fetch_instrument(eurusd().id)).id == eurusd().id

    async def test_an_unlisted_instrument_raises(self) -> None:
        source = market_data(instruments=[eurusd()])
        with pytest.raises(InstrumentNotFoundError) as caught:
            await source.fetch_instrument(btcusdt().id)
        assert caught.value.venue == "fxbroker"
        assert "cryptoex:BTC/USDT" in str(caught.value)

    async def test_quotes_are_matched_by_instrument_not_position(self) -> None:
        first = Quote(eurusd().id, NOW, Decimal("1.0850"), Decimal("1.0851"))
        source = market_data(quotes=[first])
        found = await source.fetch_quotes([btcusdt().id, eurusd().id])
        assert [item.instrument_id for item in found] == [eurusd().id]

    async def test_candles_are_filtered_by_range_and_component(self) -> None:
        bars = [
            Candle(
                eurusd().id,
                Granularity.M5,
                CandlePrice.MID,
                datetime(2025, 3, 5, hour, 0, tzinfo=UTC),
                Decimal("1.0850"),
                Decimal("1.0860"),
                Decimal("1.0840"),
                Decimal("1.0855"),
            )
            for hour in (10, 11, 12)
        ]
        source = market_data(candles=bars)
        selected = await source.fetch_candles(
            eurusd().id,
            Granularity.M5,
            start=datetime(2025, 3, 5, 11, 0, tzinfo=UTC),
            price=CandlePrice.MID,
        )
        assert [bar.start.hour for bar in selected] == [11, 12]

    async def test_incomplete_bars_are_excluded_by_default(self) -> None:
        forming = Candle(
            eurusd().id,
            Granularity.M5,
            CandlePrice.MID,
            NOW,
            Decimal("1.0850"),
            Decimal("1.0860"),
            Decimal("1.0840"),
            Decimal("1.0855"),
            complete=False,
        )
        source = market_data(candles=[forming])
        assert await source.fetch_candles(eurusd().id, Granularity.M5, price=CandlePrice.MID) == ()
        included = await source.fetch_candles(
            eurusd().id, Granularity.M5, price=CandlePrice.MID, include_incomplete=True
        )
        assert len(included) == 1

    async def test_an_unsupported_granularity_is_refused_before_the_request(self) -> None:
        source = market_data()
        with pytest.raises(UnsupportedVenueOperationError, match="1d candles"):
            await source.fetch_candles(eurusd().id, Granularity.D1, price=CandlePrice.MID)
        assert source.candle_requests == []

    async def test_streaming_yields_quotes(self) -> None:
        first = Quote(eurusd().id, NOW, Decimal("1.0850"), Decimal("1.0851"))
        source = market_data(quotes=[first])
        received = [item async for item in source.stream_quotes([eurusd().id])]
        assert received == [first]

    async def test_unsupported_operations_raise_a_venue_error(self) -> None:
        source = market_data()
        for call in (
            source.fetch_order_book(eurusd().id, 10),
            source.fetch_trades(eurusd().id),
            source.fetch_funding_rate(eurusd().id),
            source.fetch_swap_rates(eurusd().id),
        ):
            with pytest.raises(UnsupportedVenueOperationError):
                await call


class TestExecutionContract:
    def _venue(self, **kwargs: object) -> ConformingExecution:
        return ConformingExecution("fxbroker", CAPABILITIES, **kwargs)  # type: ignore[arg-type]

    async def test_account_snapshot(self) -> None:
        account = AccountSnapshot(
            account_id="a-1",
            ts=NOW,
            balance=Money.of(10_000, USD),
            equity=Money.of(10_000, USD),
            margin_used=Money.zero(USD),
            margin_available=Money.of(10_000, USD),
            unrealized_pnl=Money.zero(USD),
            realized_pnl=Money.zero(USD),
        )
        venue = self._venue(account=account)
        assert (await venue.fetch_account()).account_id == "a-1"

    async def test_capabilities_are_enforced_before_submission(self) -> None:
        venue = self._venue()
        unsupported = OrderRequest(
            client_order_id="c-1",
            instrument_id=eurusd().id,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal(1000),
            time_in_force=TimeInForce.IOC,
        )
        with pytest.raises(UnsupportedVenueOperationError):
            await venue.place_order(unsupported)
        assert venue.submitted == []

    async def test_an_unknown_order_raises(self) -> None:
        with pytest.raises(OrderNotFoundError, match="v-9"):
            await self._venue().fetch_order("v-9")

    async def test_lookup_by_client_id_returns_none_when_unseen(self) -> None:
        assert await self._venue().fetch_order_by_client_id("c-unknown") is None

    async def test_a_flat_instrument_has_no_position(self) -> None:
        assert await self._venue().fetch_position(eurusd().id) is None


class TestVenueNeutrality:
    """The interfaces must not carry any single venue's vocabulary."""

    def test_no_venue_brand_names_appear_in_the_public_surface(self) -> None:
        forbidden = ("oanda", "ctrader", "binance", "metatrader", "mt4", "mt5", "ccxt")
        for module in (base_module, enums_module, models_module):
            names = [name for name in dir(module) if not name.startswith("_")]
            for name in names:
                lowered = name.lower()
                for brand in forbidden:
                    assert brand not in lowered, f"{module.__name__}.{name} names a venue"

    def test_capabilities_express_venue_differences_instead_of_branching(self) -> None:
        # A netting forex broker and a hedging crypto venue differ by declared
        # capability, not by type.
        netting = VenueCapabilities.minimal("fxbroker", PositionMode.NETTING)
        hedging = VenueCapabilities.minimal("cryptoex", PositionMode.HEDGING)
        assert netting.position_mode is not hedging.position_mode
        assert type(netting) is type(hedging)

    def test_every_venue_error_carries_the_venue(self) -> None:
        error = UnsupportedVenueOperationError("cryptoex", "no swaps here")
        assert isinstance(error, VenueError)
        assert error.venue == "cryptoex"
        assert str(error).startswith("cryptoex: ")
