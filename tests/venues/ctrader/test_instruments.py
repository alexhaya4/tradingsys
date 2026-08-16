"""Mapping cTrader symbol metadata into the instrument model.

The tests that matter here are the ones about units. A volume read as units instead of
hundredths is a hundredfold error in every position size, and `digits` used where
`pipPosition` belongs is a factor of ten in the sizing arithmetic. Neither raises, and
neither is visible in a passing handshake, so both are asserted directly.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal

import pytest

from tradingsys.core.currency import default_registry
from tradingsys.core.financing import FinancingModel
from tradingsys.core.instrument import AssetClass, InstrumentStatus, QuantityUnit
from tradingsys.core.schedule import Weekday
from tradingsys.venues.ctrader.instruments import (
    CENTS_PER_UNIT,
    instrument_from_symbol,
    swap_convention,
)
from tradingsys.venues.ctrader.messages.OpenApiModelMessages_pb2 import (
    ProtoOADayOfWeek,
    ProtoOALightSymbol,
    ProtoOASwapCalculationType,
    ProtoOASymbol,
    ProtoOATradingMode,
)
from tradingsys.venues.errors import VenueResponseError

SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86_400


def light_symbol(name: str = "EURUSD", symbol_id: int = 1) -> ProtoOALightSymbol:
    light = ProtoOALightSymbol()
    light.symbolId = symbol_id
    light.symbolName = name
    light.baseAssetId = 10
    light.quoteAssetId = 20
    return light


def full_symbol(
    *,
    symbol_id: int = 1,
    digits: int = 5,
    pip_position: int = 4,
    min_volume: int = 100_000,
    step_volume: int = 100_000,
    max_volume: int = 1_000_000_000,
    swap_long: float = -1.2,
    swap_short: float = 0.35,
    trading_mode: ProtoOATradingMode.ValueType = ProtoOATradingMode.ENABLED,
    swap_calculation: ProtoOASwapCalculationType.ValueType = ProtoOASwapCalculationType.PIPS,
    with_schedule: bool = True,
) -> ProtoOASymbol:
    """A symbol shaped like the ones Pepperstone actually publishes.

    The default volumes are the observed ones: a minimum of 100000 hundredths, which
    is 1000 units, and a step of the same.
    """
    symbol = ProtoOASymbol()
    symbol.symbolId = symbol_id
    symbol.digits = digits
    symbol.pipPosition = pip_position
    symbol.minVolume = min_volume
    symbol.stepVolume = step_volume
    symbol.maxVolume = max_volume
    symbol.swapLong = swap_long
    symbol.swapShort = swap_short
    symbol.swapCalculationType = swap_calculation
    # The venue numbers Monday as 1, so Wednesday is 3.
    symbol.swapRollover3Days = ProtoOADayOfWeek.WEDNESDAY
    symbol.swapPeriod = 24
    symbol.swapTime = 22 * 60
    symbol.tradingMode = trading_mode
    symbol.scheduleTimeZone = "Europe/London"
    if with_schedule:
        # Sunday 22:00 through Friday 22:00, as a forex week is published.
        interval = symbol.schedule.add()
        interval.startSecond = 22 * SECONDS_PER_HOUR
        interval.endSecond = 5 * SECONDS_PER_DAY + 22 * SECONDS_PER_HOUR
    return symbol


class TestVolumesAreHundredths:
    def test_the_minimum_volume_is_divided_by_a_hundred(self) -> None:
        # 100000 hundredths is 1000 units. Reading it as units would claim a minimum
        # position a hundred times larger than the venue's, and every eligibility
        # verdict computed from it would be wrong in the direction of exclusion.
        instrument = instrument_from_symbol(
            full_symbol(min_volume=100_000),
            light_symbol(),
            default_registry,
            base_asset="EUR",
            quote_asset="USD",
        )
        assert instrument.min_quantity == Decimal(1000)

    def test_the_step_is_divided_by_a_hundred(self) -> None:
        instrument = instrument_from_symbol(
            full_symbol(step_volume=1_000),
            light_symbol(),
            default_registry,
            base_asset="EUR",
            quote_asset="USD",
        )
        assert instrument.quantity_increment == Decimal(10)

    def test_the_divisor_is_the_named_constant(self) -> None:
        assert Decimal(100) == CENTS_PER_UNIT

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_non_positive_volume_is_refused(self, bad: int) -> None:
        # A zero minimum cannot be sized against, and must not be read as "no minimum".
        with pytest.raises(VenueResponseError, match="cannot be sized against"):
            instrument_from_symbol(
                full_symbol(min_volume=bad),
                light_symbol(),
                default_registry,
                base_asset="EUR",
                quote_asset="USD",
            )


class TestPrecision:
    def test_digits_give_the_tick_and_pip_position_gives_the_pip(self) -> None:
        # On a five digit EUR/USD these are different numbers, and confusing them is a
        # factor of ten in every risk calculation.
        instrument = instrument_from_symbol(
            full_symbol(digits=5, pip_position=4),
            light_symbol(),
            default_registry,
            base_asset="EUR",
            quote_asset="USD",
        )
        assert instrument.price_increment == Decimal("0.00001")
        assert instrument.pip_size == Decimal("0.0001")
        assert instrument.price_precision == 5

    def test_a_three_digit_pair_maps_the_same_way(self) -> None:
        # USD/JPY: tick 0.001, pip 0.01.
        instrument = instrument_from_symbol(
            full_symbol(digits=3, pip_position=2),
            light_symbol(name="USDJPY"),
            default_registry,
            base_asset="USD",
            quote_asset="JPY",
        )
        assert instrument.price_increment == Decimal("0.001")
        assert instrument.pip_size == Decimal("0.01")


class TestSwap:
    def test_a_swap_double_becomes_the_decimal_the_broker_quoted(self) -> None:
        # -1.2 as a double expands to -1.19999999999999995559..., which is not a swap
        # rate anyone published. The shortest decimal that round trips is the value
        # that was meant.
        convention = swap_convention(full_symbol(swap_long=-1.2, swap_short=0.35))
        assert convention.long_rate == Decimal("-1.2")
        assert convention.short_rate == Decimal("0.35")

    def test_the_charging_convention_is_carried(self) -> None:
        convention = swap_convention(full_symbol())
        assert convention.calculated_in == "pips"
        assert convention.period_hours == 24
        assert convention.triple_day is Weekday.WEDNESDAY
        assert convention.first_charge_minutes_after_midnight_utc == 22 * 60

    def test_a_percentage_convention_is_recognised(self) -> None:
        convention = swap_convention(
            full_symbol(swap_calculation=ProtoOASwapCalculationType.PERCENTAGE)
        )
        assert convention.calculated_in == "percentage"

    def test_an_unknown_calculation_type_is_refused(self) -> None:
        # protobuf will not let an unknown value be set on a proto2 closed enum, and on
        # the wire it drops one into unknown fields so the field reads as its PIPS
        # default with HasField false. Clearing the field reproduces exactly what the
        # client would see, and it must refuse rather than read a percentage as pips.
        symbol = full_symbol()
        symbol.ClearField("swapCalculationType")
        with pytest.raises(VenueResponseError, match="orders of magnitude"):
            swap_convention(symbol)

    def test_the_rollover_time_reaches_the_financing_spec(self) -> None:
        instrument = instrument_from_symbol(
            full_symbol(),
            light_symbol(),
            default_registry,
            base_asset="EUR",
            quote_asset="USD",
        )
        assert instrument.financing.model is FinancingModel.SWAP_POINTS
        assert instrument.financing.rollover_time == time(hour=22, minute=0)
        assert instrument.financing.triple_rollover_day is Weekday.WEDNESDAY


class TestTheSchedule:
    def test_seconds_from_sunday_become_weekday_sessions(self) -> None:
        # The venue counts from Sunday and this package counts from Monday, so the
        # conversion is not the identity and an off by one day would report a gap every
        # weekend.
        instrument = instrument_from_symbol(
            full_symbol(),
            light_symbol(),
            default_registry,
            base_asset="EUR",
            quote_asset="USD",
        )
        session = instrument.schedule.sessions[0]
        assert session.open_day is Weekday.SUNDAY
        assert session.open_time == time(hour=22)
        assert session.close_day is Weekday.FRIDAY
        assert session.close_time == time(hour=22)

    def test_the_venues_timezone_is_kept(self) -> None:
        instrument = instrument_from_symbol(
            full_symbol(),
            light_symbol(),
            default_registry,
            base_asset="EUR",
            quote_asset="USD",
        )
        assert instrument.schedule.timezone == "Europe/London"

    def test_a_symbol_with_no_schedule_is_refused(self) -> None:
        # An unknown schedule and a continuously open one are different facts, and only
        # one of them is safe to assume.
        with pytest.raises(VenueResponseError, match="must not be read as continuously open"):
            instrument_from_symbol(
                full_symbol(with_schedule=False),
                light_symbol(),
                default_registry,
                base_asset="EUR",
                quote_asset="USD",
            )


class TestTradingMode:
    def test_an_enabled_symbol_is_active(self) -> None:
        instrument = instrument_from_symbol(
            full_symbol(trading_mode=ProtoOATradingMode.ENABLED),
            light_symbol(),
            default_registry,
            base_asset="EUR",
            quote_asset="USD",
        )
        assert instrument.status is InstrumentStatus.ACTIVE

    def test_close_only_is_reduce_only_and_not_halted(self) -> None:
        # The difference decides whether the kill switch can flatten. Collapsing the
        # two would either stop a flatten that would have worked or accept an entry the
        # venue would refuse.
        instrument = instrument_from_symbol(
            full_symbol(trading_mode=ProtoOATradingMode.CLOSE_ONLY_MODE),
            light_symbol(),
            default_registry,
            base_asset="EUR",
            quote_asset="USD",
        )
        assert instrument.status is InstrumentStatus.REDUCE_ONLY

    @pytest.mark.parametrize(
        "mode",
        [
            ProtoOATradingMode.DISABLED_WITHOUT_PENDINGS_EXECUTION,
            ProtoOATradingMode.DISABLED_WITH_PENDINGS_EXECUTION,
        ],
    )
    def test_a_disabled_symbol_is_halted(self, mode: ProtoOATradingMode.ValueType) -> None:
        instrument = instrument_from_symbol(
            full_symbol(trading_mode=mode),
            light_symbol(),
            default_registry,
            base_asset="EUR",
            quote_asset="USD",
        )
        assert instrument.status is InstrumentStatus.HALTED

    def test_an_unknown_mode_is_refused_rather_than_assumed_tradeable(self) -> None:
        # An unrecognised mode arrives as an absent field reading as ENABLED, which is
        # the one answer that must not be given. Clearing the field reproduces it.
        symbol = full_symbol()
        symbol.ClearField("tradingMode")
        with pytest.raises(VenueResponseError, match="may have closed"):
            instrument_from_symbol(
                symbol,
                light_symbol(),
                default_registry,
                base_asset="EUR",
                quote_asset="USD",
            )


class TestTheInstrumentItself:
    def test_it_carries_the_venue_symbol_and_a_canonical_id(self) -> None:
        instrument = instrument_from_symbol(
            full_symbol(),
            light_symbol(name="EURUSD"),
            default_registry,
            base_asset="EUR",
            quote_asset="USD",
        )
        assert instrument.venue_symbol == "EURUSD"
        assert str(instrument.id) == "ctrader:EUR/USD"
        assert instrument.asset_class is AssetClass.FX_SPOT
        assert instrument.quantity_unit is QuantityUnit.UNITS
        assert instrument.contract_size == Decimal(1)

    def test_a_margin_pair_settles_in_the_quote(self) -> None:
        instrument = instrument_from_symbol(
            full_symbol(),
            light_symbol(name="USDJPY"),
            default_registry,
            base_asset="USD",
            quote_asset="JPY",
        )
        assert instrument.settlement_currency.code == "JPY"

    def test_no_notional_minimum_is_invented(self) -> None:
        # The venue publishes none for these symbols. Asserting one would exclude
        # instruments for a limit that does not exist.
        instrument = instrument_from_symbol(
            full_symbol(),
            light_symbol(),
            default_registry,
            base_asset="EUR",
            quote_asset="USD",
        )
        assert instrument.min_notional is None
