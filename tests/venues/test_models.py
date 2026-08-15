"""Tests for the venue value objects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from tests.factories import USD, USDT, btcusdt, btcusdt_perp, eurusd, eurusd_lots
from tradingsys.core.errors import (
    DomainError,
    InvalidPriceError,
    InvalidQuantityError,
    PipNotDefinedError,
)
from tradingsys.core.instrument import InstrumentId
from tradingsys.core.money import Money
from tradingsys.venues.enums import (
    CandlePrice,
    Granularity,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionMode,
    PositionSide,
    TimeInForce,
)
from tradingsys.venues.errors import UnsupportedVenueOperationError
from tradingsys.venues.models import (
    AccountSnapshot,
    BookLevel,
    Candle,
    ExecutionEvent,
    Fill,
    FundingRate,
    Order,
    OrderBook,
    OrderRequest,
    Position,
    Quote,
    SwapRates,
    Trade,
    VenueCapabilities,
)

NOW = datetime(2025, 3, 5, 12, 0, tzinfo=UTC)
EURUSD = eurusd().id
BTC = btcusdt().id


def quote(**overrides: object) -> Quote:
    defaults: dict[str, object] = {
        "instrument_id": EURUSD,
        "ts": NOW,
        "bid": Decimal("1.08500"),
        "ask": Decimal("1.08512"),
    }
    defaults.update(overrides)
    return Quote(**defaults)  # type: ignore[arg-type]


class TestQuote:
    def test_mid_and_spread(self) -> None:
        assert quote().mid == Decimal("1.08506")
        assert quote().spread == Decimal("0.00012")

    def test_spread_in_pips(self) -> None:
        assert quote().spread_in_pips(eurusd()) == Decimal("1.2")

    def test_spread_in_pips_needs_a_pip_convention(self) -> None:
        crypto = quote(instrument_id=BTC, bid=Decimal("60000"), ask=Decimal("60001"))
        with pytest.raises(PipNotDefinedError):
            crypto.spread_in_pips(btcusdt())

    def test_price_for_side(self) -> None:
        assert quote().price_for(OrderSide.BUY) == Decimal("1.08512")
        assert quote().price_for(OrderSide.SELL) == Decimal("1.08500")

    def test_timestamps_are_normalised_to_utc(self) -> None:
        tokyo = datetime(2025, 3, 5, 21, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        assert quote(ts=tokyo).ts == datetime(2025, 3, 5, 12, 0, tzinfo=UTC)

    def test_naive_timestamps_are_rejected(self) -> None:
        with pytest.raises(DomainError, match="timezone aware"):
            quote(ts=datetime(2025, 3, 5, 12, 0))  # noqa: DTZ001

    def test_crossed_quotes_are_rejected(self) -> None:
        with pytest.raises(DomainError, match="crossed quote"):
            quote(bid=Decimal("1.09"), ask=Decimal("1.08"))

    def test_a_locked_quote_is_allowed(self) -> None:
        # Bid equal to ask happens legitimately at a session open.
        assert quote(bid=Decimal("1.085"), ask=Decimal("1.085")).spread == 0

    def test_non_positive_prices_are_rejected(self) -> None:
        with pytest.raises(DomainError, match="bid must be positive"):
            quote(bid=Decimal(0))

    def test_negative_sizes_are_rejected(self) -> None:
        with pytest.raises(DomainError, match="bid_size must not be negative"):
            quote(bid_size=Decimal(-1))

    def test_float_prices_are_rejected(self) -> None:
        with pytest.raises(TypeError, match="must not be a float"):
            quote(bid=1.085)

    def test_string_prices_are_parsed_exactly(self) -> None:
        assert quote(bid="1.08500").bid == Decimal("1.08500")

    def test_untradeable_quotes_are_representable(self) -> None:
        assert quote(tradeable=False).tradeable is False


class TestTrade:
    def test_valid_trade(self) -> None:
        trade = Trade(BTC, NOW, Decimal("60000.5"), Decimal("0.25"), OrderSide.BUY, "t-1")
        assert trade.aggressor is OrderSide.BUY

    def test_non_positive_size_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="size must be positive"):
            Trade(BTC, NOW, Decimal(60000), Decimal(0))

    def test_non_positive_price_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="price must be positive"):
            Trade(BTC, NOW, Decimal(0), Decimal(1))


class TestOrderBook:
    def test_best_levels_and_quote_conversion(self) -> None:
        book = OrderBook(
            BTC,
            NOW,
            bids=(BookLevel(Decimal(60000), Decimal(1)), BookLevel(Decimal(59999), Decimal(2))),
            asks=(BookLevel(Decimal(60001), Decimal(1)), BookLevel(Decimal(60002), Decimal(3))),
        )
        assert book.best_bid == BookLevel(Decimal(60000), Decimal(1))
        assert book.best_ask == BookLevel(Decimal(60001), Decimal(1))
        top = book.to_quote()
        assert top is not None
        assert top.bid == Decimal(60000)
        assert top.ask == Decimal(60001)

    def test_unordered_bids_are_rejected(self) -> None:
        with pytest.raises(DomainError, match="highest price first"):
            OrderBook(
                BTC,
                NOW,
                bids=(BookLevel(Decimal(59999), Decimal(1)), BookLevel(Decimal(60000), Decimal(1))),
                asks=(),
            )

    def test_unordered_asks_are_rejected(self) -> None:
        with pytest.raises(DomainError, match="lowest price first"):
            OrderBook(
                BTC,
                NOW,
                bids=(),
                asks=(BookLevel(Decimal(60002), Decimal(1)), BookLevel(Decimal(60001), Decimal(1))),
            )

    def test_a_crossed_book_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="crossed book"):
            OrderBook(
                BTC,
                NOW,
                bids=(BookLevel(Decimal(60002), Decimal(1)),),
                asks=(BookLevel(Decimal(60001), Decimal(1)),),
            )

    def test_an_empty_side_yields_no_quote(self) -> None:
        assert OrderBook(BTC, NOW, bids=(), asks=()).to_quote() is None

    def test_non_positive_level_size_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="size must be positive"):
            BookLevel(Decimal(1), Decimal(0))


def candle(**overrides: object) -> Candle:
    defaults: dict[str, object] = {
        "instrument_id": EURUSD,
        "granularity": Granularity.M5,
        "price": CandlePrice.MID,
        "start": NOW,
        "open": Decimal("1.0850"),
        "high": Decimal("1.0860"),
        "low": Decimal("1.0845"),
        "close": Decimal("1.0855"),
    }
    defaults.update(overrides)
    return Candle(**defaults)  # type: ignore[arg-type]


class TestCandle:
    def test_interval_is_half_open_from_the_start(self) -> None:
        bar = candle()
        assert bar.end == NOW + timedelta(minutes=5)
        assert bar.contains(NOW)
        assert bar.contains(NOW + timedelta(minutes=4, seconds=59))
        assert not bar.contains(bar.end)

    def test_range(self) -> None:
        assert candle().range == Decimal("0.0015")

    def test_a_high_below_the_body_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="high"):
            candle(high=Decimal("1.0850"), close=Decimal("1.0855"))

    def test_a_low_above_the_body_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="low"):
            candle(low=Decimal("1.0855"), open=Decimal("1.0850"))

    def test_a_flat_bar_is_valid(self) -> None:
        flat = candle(
            open=Decimal("1.0850"),
            high=Decimal("1.0850"),
            low=Decimal("1.0850"),
            close=Decimal("1.0850"),
        )
        assert flat.range == 0

    def test_negative_volume_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="volume"):
            candle(volume=Decimal(-1))

    def test_negative_trade_count_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="trade_count"):
            candle(trade_count=-1)

    def test_incomplete_bars_are_flagged(self) -> None:
        assert candle(complete=False).complete is False


class TestFundingAndSwap:
    def test_funding_charge_is_signed(self) -> None:
        rate = FundingRate(BTC, NOW, Decimal("0.0001"), timedelta(hours=8))
        notional = Money.of(10_000, USDT)
        assert rate.charge_on(notional) == Money.of("1.0000", USDT)
        negative = FundingRate(BTC, NOW, Decimal("-0.0001"), timedelta(hours=8))
        assert negative.charge_on(notional).is_negative

    def test_funding_interval_must_be_positive(self) -> None:
        with pytest.raises(DomainError, match="interval must be positive"):
            FundingRate(BTC, NOW, Decimal("0.0001"), timedelta(0))

    def test_swap_charge_uses_base_units_and_nights(self) -> None:
        rates = SwapRates(EURUSD, NOW, Decimal("-0.00002"), Decimal("0.00001"))
        charge = rates.charge(eurusd(), Decimal(100_000), PositionSide.LONG, nights=3)
        assert charge == Money.of("-6.00000", USD)

    def test_swap_charge_respects_lot_sizing(self) -> None:
        rates = SwapRates(eurusd_lots().id, NOW, Decimal("-0.00002"), Decimal("0.00001"))
        by_lot = rates.charge(eurusd_lots(), Decimal(1), PositionSide.LONG)
        by_unit = rates.charge(eurusd(), Decimal(100_000), PositionSide.LONG)
        assert by_lot == by_unit

    def test_a_flat_position_accrues_no_swap(self) -> None:
        rates = SwapRates(EURUSD, NOW, Decimal("-0.00002"), Decimal("0.00001"))
        assert rates.points_for(PositionSide.FLAT) == 0

    def test_negative_nights_are_rejected(self) -> None:
        rates = SwapRates(EURUSD, NOW, Decimal("-0.00002"), Decimal("0.00001"))
        with pytest.raises(DomainError, match="nights"):
            rates.charge(eurusd(), Decimal(1), PositionSide.LONG, nights=-1)


def request(**overrides: object) -> OrderRequest:
    defaults: dict[str, object] = {
        "client_order_id": "c-1",
        "instrument_id": EURUSD,
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal(1000),
    }
    defaults.update(overrides)
    return OrderRequest(**defaults)  # type: ignore[arg-type]


class TestOrderRequest:
    def test_a_market_order_needs_nothing_else(self) -> None:
        assert request().order_type is OrderType.MARKET

    def test_quantity_must_be_positive_and_unsigned(self) -> None:
        with pytest.raises(DomainError, match="unsigned"):
            request(quantity=Decimal(-1000))

    def test_client_order_id_must_be_meaningful(self) -> None:
        with pytest.raises(DomainError, match="client_order_id"):
            request(client_order_id="")
        with pytest.raises(DomainError, match="client_order_id"):
            request(client_order_id=" c-1 ")

    def test_a_limit_order_needs_a_limit_price(self) -> None:
        with pytest.raises(DomainError, match="needs a limit price"):
            request(order_type=OrderType.LIMIT)
        assert request(order_type=OrderType.LIMIT, limit_price=Decimal("1.08")).limit_price

    def test_a_market_order_must_not_carry_a_limit_price(self) -> None:
        with pytest.raises(DomainError, match="must not carry a limit price"):
            request(limit_price=Decimal("1.08"))

    def test_a_stop_order_needs_a_trigger(self) -> None:
        with pytest.raises(DomainError, match="needs a trigger price"):
            request(order_type=OrderType.STOP)

    def test_a_stop_limit_order_needs_both(self) -> None:
        with pytest.raises(DomainError, match="needs a limit price"):
            request(order_type=OrderType.STOP_LIMIT, trigger_price=Decimal("1.09"))
        order = request(
            order_type=OrderType.STOP_LIMIT,
            trigger_price=Decimal("1.09"),
            limit_price=Decimal("1.091"),
        )
        assert order.trigger_price == Decimal("1.09")

    def test_a_trailing_stop_needs_a_distance(self) -> None:
        with pytest.raises(DomainError, match="needs a trailing distance"):
            request(order_type=OrderType.TRAILING_STOP)

    def test_a_non_trailing_order_must_not_carry_a_distance(self) -> None:
        with pytest.raises(DomainError, match="must not carry a trailing distance"):
            request(trailing_distance=Decimal("0.001"))

    def test_gtd_needs_an_expiry(self) -> None:
        with pytest.raises(DomainError, match="needs an expiry"):
            request(time_in_force=TimeInForce.GTD)
        assert request(time_in_force=TimeInForce.GTD, expires_at=NOW).expires_at == NOW

    def test_a_non_gtd_order_must_not_carry_an_expiry(self) -> None:
        with pytest.raises(DomainError, match="must not carry an expiry"):
            request(expires_at=NOW)

    def test_post_only_is_limit_only(self) -> None:
        with pytest.raises(DomainError, match="post_only only applies to limit"):
            request(post_only=True)

    def test_post_only_contradicts_an_immediate_time_in_force(self) -> None:
        with pytest.raises(DomainError, match="contradicts"):
            request(
                order_type=OrderType.LIMIT,
                limit_price=Decimal("1.08"),
                time_in_force=TimeInForce.IOC,
                post_only=True,
            )

    def test_negative_prices_are_rejected(self) -> None:
        with pytest.raises(DomainError, match="limit_price must be positive"):
            request(order_type=OrderType.LIMIT, limit_price=Decimal(-1))

    def test_validate_against_checks_the_instrument_grid(self) -> None:
        with pytest.raises(InvalidQuantityError):
            request(instrument_id=eurusd_lots().id, quantity=Decimal("0.015")).validate_against(
                eurusd_lots()
            )
        with pytest.raises(InvalidPriceError):
            request(order_type=OrderType.LIMIT, limit_price=Decimal("1.0850123")).validate_against(
                eurusd()
            )

    def test_validate_against_the_wrong_instrument_is_an_error(self) -> None:
        with pytest.raises(DomainError, match="was checked against"):
            request().validate_against(btcusdt())

    def test_a_valid_request_passes_instrument_validation(self) -> None:
        request(order_type=OrderType.LIMIT, limit_price=Decimal("1.08501")).validate_against(
            eurusd()
        )


def order(**overrides: object) -> Order:
    defaults: dict[str, object] = {
        "venue_order_id": "v-1",
        "client_order_id": "c-1",
        "instrument_id": EURUSD,
        "side": OrderSide.BUY,
        "order_type": OrderType.LIMIT,
        "status": OrderStatus.OPEN,
        "quantity": Decimal(1000),
        "filled_quantity": Decimal(0),
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(overrides)
    return Order(**defaults)  # type: ignore[arg-type]


class TestOrder:
    def test_remaining_quantity(self) -> None:
        assert order(filled_quantity=Decimal(400)).remaining_quantity == Decimal(600)

    def test_overfill_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="exceeds ordered"):
            order(filled_quantity=Decimal(1001))

    def test_a_filled_status_must_match_the_filled_quantity(self) -> None:
        with pytest.raises(DomainError, match="status is filled"):
            order(status=OrderStatus.FILLED, filled_quantity=Decimal(500))

    def test_updated_before_created_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="precedes created_at"):
            order(updated_at=NOW - timedelta(seconds=1))

    def test_working_and_terminal_states(self) -> None:
        assert order(status=OrderStatus.OPEN).is_working
        assert order(status=OrderStatus.FILLED, filled_quantity=Decimal(1000)).is_terminal
        assert not order(status=OrderStatus.CANCELLED).is_working

    def test_partially_filled_is_still_working(self) -> None:
        assert order(status=OrderStatus.PARTIALLY_FILLED, filled_quantity=Decimal(1)).is_working


class TestFill:
    def test_notional_uses_the_instrument_contract_size(self) -> None:
        fill = Fill(
            "f-1",
            "v-1",
            "c-1",
            eurusd_lots().id,
            OrderSide.BUY,
            Decimal("0.5"),
            Decimal("1.0850"),
            NOW,
        )
        assert fill.notional(eurusd_lots()) == Money.of("54250.000", USD)

    def test_fees_carry_their_own_currency(self) -> None:
        fill = Fill(
            "f-1",
            "v-1",
            "c-1",
            BTC,
            OrderSide.BUY,
            Decimal("0.5"),
            Decimal(60000),
            NOW,
            fee=Money.of("0.0005", btcusdt().base_currency),
        )
        assert fill.fee is not None
        assert fill.fee.currency.code == "BTC"

    def test_non_positive_quantity_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="quantity must be positive"):
            Fill("f-1", "v-1", "c-1", EURUSD, OrderSide.BUY, Decimal(0), Decimal(1), NOW)


class TestPosition:
    def _position(self, **overrides: object) -> Position:
        defaults: dict[str, object] = {
            "instrument_id": EURUSD,
            "side": PositionSide.LONG,
            "quantity": Decimal(10_000),
            "average_price": Decimal("1.0850"),
            "unrealized_pnl": Money.of(12, USD),
            "realized_pnl": Money.zero(USD),
            "margin_used": Money.of(360, USD),
            "updated_at": NOW,
        }
        defaults.update(overrides)
        return Position(**defaults)  # type: ignore[arg-type]

    def test_signed_quantity(self) -> None:
        assert self._position().signed_quantity == Decimal(10_000)
        assert self._position(side=PositionSide.SHORT).signed_quantity == Decimal(-10_000)

    def test_closing_side_is_the_opposite(self) -> None:
        assert self._position().closing_side() is OrderSide.SELL
        assert self._position(side=PositionSide.SHORT).closing_side() is OrderSide.BUY

    def test_a_flat_position_has_nothing_to_close(self) -> None:
        flat = self._position(side=PositionSide.FLAT, quantity=Decimal(0))
        assert flat.is_flat
        assert flat.signed_quantity == 0
        with pytest.raises(DomainError, match="nothing to close"):
            flat.closing_side()

    def test_quantity_must_be_unsigned(self) -> None:
        with pytest.raises(DomainError, match="unsigned"):
            self._position(quantity=Decimal(-1))

    def test_a_flat_position_must_be_empty(self) -> None:
        with pytest.raises(DomainError, match="flat position must have zero"):
            self._position(side=PositionSide.FLAT)

    def test_a_directional_position_must_not_be_empty(self) -> None:
        with pytest.raises(DomainError, match="non-zero quantity"):
            self._position(quantity=Decimal(0))

    def test_side_from_signed_quantity(self) -> None:
        assert PositionSide.from_signed_quantity(Decimal(5)) is PositionSide.LONG
        assert PositionSide.from_signed_quantity(Decimal(-5)) is PositionSide.SHORT
        assert PositionSide.from_signed_quantity(Decimal(0)) is PositionSide.FLAT


class TestAccountSnapshot:
    def _account(self, **overrides: object) -> AccountSnapshot:
        defaults: dict[str, object] = {
            "account_id": "a-1",
            "ts": NOW,
            "balance": Money.of(10_000, USD),
            "equity": Money.of(10_012, USD),
            "margin_used": Money.of(2_000, USD),
            "margin_available": Money.of(8_012, USD),
            "unrealized_pnl": Money.of(12, USD),
            "realized_pnl": Money.zero(USD),
        }
        defaults.update(overrides)
        return AccountSnapshot(**defaults)  # type: ignore[arg-type]

    def test_margin_utilisation(self) -> None:
        utilisation = self._account().margin_utilisation
        assert utilisation.quantize(Decimal("0.0001")) == Decimal("0.1998")

    def test_zero_equity_yields_zero_utilisation_rather_than_an_error(self) -> None:
        blown = self._account(equity=Money.zero(USD))
        assert blown.margin_utilisation == 0

    def test_mixed_currencies_are_rejected(self) -> None:
        with pytest.raises(DomainError, match="not denominated in the account currency"):
            self._account(margin_used=Money.of(2_000, USDT))

    def test_every_mismatched_field_is_named(self) -> None:
        with pytest.raises(DomainError) as caught:
            self._account(
                margin_used=Money.of(1, USDT),
                realized_pnl=Money.of(1, USDT),
            )
        assert "margin_used" in str(caught.value)
        assert "realized_pnl" in str(caught.value)

    def test_position_mode_is_recorded(self) -> None:
        assert self._account(position_mode=PositionMode.HEDGING).position_mode is (
            PositionMode.HEDGING
        )


class TestExecutionEvent:
    def test_an_empty_event_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="at least one of"):
            ExecutionEvent(ts=NOW, venue="fxbroker")

    def test_an_event_carrying_an_order_is_valid(self) -> None:
        event = ExecutionEvent(ts=NOW, venue="fxbroker", order=order(), sequence=17)
        assert event.order is not None
        assert event.sequence == 17


class TestVenueCapabilities:
    def _caps(self, **overrides: object) -> VenueCapabilities:
        defaults: dict[str, object] = {
            "venue": "fxbroker",
            "position_mode": PositionMode.NETTING,
            "order_types": frozenset({OrderType.MARKET, OrderType.LIMIT}),
            "time_in_force": frozenset({TimeInForce.GTC, TimeInForce.FOK}),
            "granularities": frozenset({Granularity.M5}),
            "candle_prices": frozenset({CandlePrice.BID, CandlePrice.ASK, CandlePrice.MID}),
            "max_candles_per_request": 500,
            "supports_historical_candles": True,
        }
        defaults.update(overrides)
        return VenueCapabilities(**defaults)  # type: ignore[arg-type]

    def test_a_supported_request_passes(self) -> None:
        assert self._caps().supports(request())

    def test_an_unsupported_order_type_is_reported(self) -> None:
        with pytest.raises(UnsupportedVenueOperationError, match="order type stop"):
            self._caps().require_supported(
                request(order_type=OrderType.STOP, trigger_price=Decimal("1.09"))
            )

    def test_an_unsupported_time_in_force_is_reported(self) -> None:
        with pytest.raises(UnsupportedVenueOperationError, match="time in force ioc"):
            self._caps().require_supported(request(time_in_force=TimeInForce.IOC))

    def test_unsupported_flags_are_reported(self) -> None:
        with pytest.raises(UnsupportedVenueOperationError, match="reduce_only"):
            self._caps().require_supported(request(reduce_only=True))
        with pytest.raises(UnsupportedVenueOperationError, match="attached take profit"):
            self._caps().require_supported(request(take_profit_price=Decimal("1.10")))

    def test_every_problem_is_reported_at_once(self) -> None:
        with pytest.raises(UnsupportedVenueOperationError) as caught:
            self._caps().require_supported(request(time_in_force=TimeInForce.IOC, reduce_only=True))
        message = str(caught.value)
        assert "time in force ioc" in message
        assert "reduce_only" in message

    def test_supports_returns_false_instead_of_raising(self) -> None:
        assert not self._caps().supports(request(reduce_only=True))

    def test_granularity_checks(self) -> None:
        caps = self._caps()
        caps.require_granularity(Granularity.M5, CandlePrice.BID)
        with pytest.raises(UnsupportedVenueOperationError, match="does not publish 1h candles"):
            caps.require_granularity(Granularity.H1, CandlePrice.BID)
        with pytest.raises(UnsupportedVenueOperationError, match="does not publish trade candles"):
            caps.require_granularity(Granularity.M5, CandlePrice.TRADE)

    def test_a_venue_without_history_says_so(self) -> None:
        caps = self._caps(
            supports_historical_candles=False,
            granularities=frozenset(),
            candle_prices=frozenset(),
        )
        with pytest.raises(
            UnsupportedVenueOperationError, match="does not publish historical candles"
        ):
            caps.require_granularity(Granularity.M5, CandlePrice.MID)

    def test_declaring_history_without_granularities_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="no granularity is listed"):
            self._caps(granularities=frozenset())

    def test_a_venue_must_support_some_order_type(self) -> None:
        with pytest.raises(DomainError, match="at least one order type"):
            self._caps(order_types=frozenset())

    def test_page_size_must_be_positive(self) -> None:
        with pytest.raises(DomainError, match="max_candles_per_request"):
            self._caps(max_candles_per_request=0)

    def test_minimal_capabilities_are_valid(self) -> None:
        minimal = VenueCapabilities.minimal("somewhere")
        assert minimal.supports(request(time_in_force=TimeInForce.IOC))
        assert not minimal.supports(
            request(order_type=OrderType.LIMIT, limit_price=Decimal("1.08"))
        )


class TestInstrumentScoping:
    def test_models_carry_venue_scoped_ids(self) -> None:
        # The same symbol at two venues must never collide in a keyed collection.
        prices = {
            eurusd().id: quote(),
            eurusd_lots().id: quote(instrument_id=eurusd_lots().id),
        }
        assert len(prices) == 2
        assert InstrumentId("fxbroker", "EUR/USD") in prices

    def test_perpetual_and_spot_are_distinct_instruments(self) -> None:
        assert btcusdt().id != btcusdt_perp().id
