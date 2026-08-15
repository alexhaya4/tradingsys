"""Tests for instrument definitions, sizing, and valuation."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, time
from decimal import Decimal

import pytest

from tests.factories import (
    CRYPTO_WEEK,
    EUR,
    FOREX_WEEK,
    JPY,
    USD,
    USDT,
    btcusdt,
    btcusdt_perp,
    eurusd,
    eurusd_lots,
    usdjpy,
)
from tradingsys.core.currency import default_registry
from tradingsys.core.errors import (
    InstrumentDefinitionError,
    InvalidPriceError,
    InvalidQuantityError,
    PipNotDefinedError,
)
from tradingsys.core.financing import FinancingSpec
from tradingsys.core.instrument import (
    AssetClass,
    Instrument,
    InstrumentId,
    InstrumentStatus,
    QuantityUnit,
)
from tradingsys.core.money import Money
from tradingsys.core.rounding import Rounding


class TestInstrumentId:
    def test_string_form(self) -> None:
        assert str(InstrumentId("oanda", "EUR/USD")) == "oanda:EUR/USD"

    def test_rejects_empty_parts(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="venue must be non-empty"):
            InstrumentId("", "EUR/USD")
        with pytest.raises(InstrumentDefinitionError, match="symbol must be non-empty"):
            InstrumentId("oanda", "")

    def test_rejects_padded_parts(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="unpadded"):
            InstrumentId("oanda", " EUR/USD")

    def test_same_symbol_at_different_venues_is_a_different_instrument(self) -> None:
        assert InstrumentId("a", "EUR/USD") != InstrumentId("b", "EUR/USD")

    def test_is_hashable_and_orderable(self) -> None:
        ids = {InstrumentId("b", "X"), InstrumentId("a", "Y")}
        assert sorted(ids) == [InstrumentId("a", "Y"), InstrumentId("b", "X")]


class TestValidation:
    def _build(self, **overrides: object) -> Instrument:
        defaults: dict[str, object] = {
            "id": InstrumentId("venue", "EUR/USD"),
            "venue_symbol": "EUR_USD",
            "asset_class": AssetClass.FX_SPOT,
            "base_currency": EUR,
            "quote_currency": USD,
            "settlement_currency": USD,
            "price_increment": Decimal("0.00001"),
            "price_precision": 5,
            "pip_size": Decimal("0.0001"),
            "quantity_unit": QuantityUnit.UNITS,
            "contract_size": Decimal(1),
            "quantity_increment": Decimal(1),
            "min_quantity": Decimal(1),
            "financing": FinancingSpec.none(),
            "schedule": FOREX_WEEK,
        }
        defaults.update(overrides)
        return Instrument(**defaults)  # type: ignore[arg-type]

    def test_valid_definition_builds(self) -> None:
        assert self._build().symbol == "EUR/USD"

    def test_rejects_identical_base_and_quote(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="both USD"):
            self._build(base_currency=USD)

    def test_rejects_foreign_settlement_currency(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="settlement currency"):
            self._build(settlement_currency=JPY)

    def test_rejects_non_positive_tick(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="price_increment must be positive"):
            self._build(price_increment=Decimal(0))

    def test_rejects_precision_narrower_than_the_tick(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="more decimal places"):
            self._build(price_increment=Decimal("0.00001"), price_precision=3)

    def test_rejects_negative_precision(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="price_precision must not be negative"):
            self._build(price_precision=-1)

    def test_rejects_pip_that_is_not_a_multiple_of_the_tick(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="not a whole multiple"):
            self._build(price_increment=Decimal("0.00003"), pip_size=Decimal("0.0001"))

    def test_rejects_non_positive_pip(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="pip_size must be positive"):
            self._build(pip_size=Decimal(0))

    def test_rejects_contract_size_on_unit_sized_instruments(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="contract_size 1"):
            self._build(quantity_unit=QuantityUnit.UNITS, contract_size=Decimal(100_000))

    def test_rejects_non_positive_contract_size(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="contract_size must be positive"):
            self._build(quantity_unit=QuantityUnit.LOTS, contract_size=Decimal(0))

    def test_rejects_minimum_off_the_step_grid(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="min_quantity"):
            self._build(quantity_increment=Decimal("0.5"), min_quantity=Decimal("0.7"))

    def test_rejects_maximum_below_minimum(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="below min_quantity"):
            self._build(min_quantity=Decimal(10), max_quantity=Decimal(5))

    def test_rejects_maximum_off_the_step_grid(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="max_quantity"):
            self._build(
                quantity_increment=Decimal(5), min_quantity=Decimal(5), max_quantity=Decimal(12)
            )

    def test_rejects_min_notional_in_the_wrong_currency(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="denominated in"):
            self._build(min_notional=Money.of(5, JPY))

    def test_rejects_non_positive_leverage(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="max_leverage must be positive"):
            self._build(max_leverage=Decimal(0))

    def test_is_frozen(self) -> None:
        instrument = self._build()
        with pytest.raises(AttributeError):
            instrument.price_increment = Decimal("0.1")  # type: ignore[misc]

    def test_rejects_an_empty_venue_symbol(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="venue_symbol"):
            self._build(venue_symbol="")

    def test_rejects_a_padded_venue_symbol(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="unpadded"):
            self._build(venue_symbol=" EUR_USD ")


class TestVenueSymbolMapping:
    def test_the_venue_spelling_is_kept_alongside_the_canonical_one(self) -> None:
        # Every venue spells the same pair differently, and the transformation is not
        # a rule an adapter can derive.
        assert eurusd().symbol == "EUR/USD"
        assert eurusd().venue_symbol == "EUR_USD"
        assert eurusd_lots().symbol == "EUR/USD"
        assert eurusd_lots().venue_symbol == "EURUSD"

    def test_the_canonical_symbol_is_shared_across_venues(self) -> None:
        assert eurusd().symbol == eurusd_lots().symbol
        assert eurusd().venue_symbol != eurusd_lots().venue_symbol

    def test_the_mapping_survives_serialisation(self) -> None:
        restored = Instrument.from_mapping(eurusd().to_mapping(), default_registry)
        assert restored.venue_symbol == "EUR_USD"


class TestSizing:
    def test_unit_sized_conversion_is_the_identity(self) -> None:
        assert eurusd().to_base_units(Decimal(25_000)) == Decimal(25_000)
        assert eurusd().from_base_units(Decimal(25_000)) == Decimal(25_000)

    def test_lot_sized_conversion(self) -> None:
        instrument = eurusd_lots()
        assert instrument.to_base_units(Decimal("0.5")) == Decimal(50_000)
        assert instrument.from_base_units(Decimal(50_000)) == Decimal("0.5")

    def test_contract_sized_conversion(self) -> None:
        assert btcusdt_perp().to_base_units(Decimal(250)) == Decimal("0.250")

    def test_round_trip_through_base_units(self) -> None:
        instrument = eurusd_lots()
        assert instrument.from_base_units(instrument.to_base_units(Decimal("1.23"))) == Decimal(
            "1.23"
        )

    def test_conversion_rejects_floats(self) -> None:
        with pytest.raises(TypeError, match="must not be a float"):
            eurusd().to_base_units(1.5)  # type: ignore[arg-type]


class TestQuantityValidation:
    def test_accepts_a_valid_quantity(self) -> None:
        assert eurusd().validate_quantity(Decimal(1000)) == Decimal(1000)

    def test_direction_is_ignored(self) -> None:
        assert eurusd().validate_quantity(Decimal(-1000)) == Decimal(-1000)

    def test_rejects_zero(self) -> None:
        with pytest.raises(InvalidQuantityError, match="must not be zero"):
            eurusd().validate_quantity(0)

    def test_rejects_below_minimum(self) -> None:
        with pytest.raises(InvalidQuantityError, match="below the minimum"):
            eurusd_lots().validate_quantity(Decimal("0.001"))

    def test_rejects_above_maximum(self) -> None:
        with pytest.raises(InvalidQuantityError, match="exceeds the maximum"):
            eurusd_lots().validate_quantity(Decimal(101))

    def test_rejects_off_grid(self) -> None:
        with pytest.raises(InvalidQuantityError, match="not a whole multiple"):
            eurusd_lots().validate_quantity(Decimal("0.015"))

    def test_predicate_form(self) -> None:
        assert eurusd_lots().is_valid_quantity(Decimal("0.02"))
        assert not eurusd_lots().is_valid_quantity(Decimal("0.015"))

    def test_crypto_step_precision(self) -> None:
        instrument = btcusdt()
        assert instrument.is_valid_quantity(Decimal("0.00123"))
        assert not instrument.is_valid_quantity(Decimal("0.000001"))


class TestQuantization:
    def test_quantity_rounds_down_by_default(self) -> None:
        assert eurusd_lots().quantize_quantity(Decimal("0.019")) == Decimal("0.01")

    def test_quantity_rounding_mode_is_selectable(self) -> None:
        instrument = eurusd_lots()
        assert instrument.quantize_quantity(Decimal("0.019"), Rounding.UP) == Decimal("0.02")
        assert instrument.quantize_quantity(Decimal("0.015"), Rounding.HALF_EVEN) == Decimal("0.02")

    def test_quantized_quantity_is_always_acceptable(self) -> None:
        instrument = btcusdt()
        snapped = instrument.quantize_quantity(Decimal("0.123456789"))
        assert snapped == Decimal("0.12345")
        assert instrument.is_valid_quantity(snapped)

    def test_price_snaps_to_the_tick_grid(self) -> None:
        assert eurusd().quantize_price(Decimal("1.234567")) == Decimal("1.23457")
        assert usdjpy().quantize_price(Decimal("151.23456")) == Decimal("151.235")

    def test_quantized_price_carries_the_declared_precision(self) -> None:
        assert str(eurusd().quantize_price(Decimal("1.2"))) == "1.20000"

    def test_price_rounding_mode_is_selectable(self) -> None:
        assert eurusd().quantize_price(Decimal("1.234565"), Rounding.DOWN) == Decimal("1.23456")
        assert eurusd().quantize_price(Decimal("1.234561"), Rounding.UP) == Decimal("1.23457")


class TestPriceValidation:
    def test_accepts_on_grid_price(self) -> None:
        assert eurusd().validate_price(Decimal("1.23456")) == Decimal("1.23456")

    def test_rejects_off_grid_price(self) -> None:
        with pytest.raises(InvalidPriceError, match="not a whole multiple"):
            eurusd().validate_price(Decimal("1.234567"))

    def test_rejects_non_positive_price(self) -> None:
        with pytest.raises(InvalidPriceError, match="must be positive"):
            eurusd().validate_price(Decimal(0))


class TestValuation:
    def test_notional_of_a_unit_sized_position(self) -> None:
        assert eurusd().notional(Decimal(10_000), Decimal("1.0850")) == Money.of("10850.00", USD)

    def test_notional_of_a_lot_sized_position(self) -> None:
        assert eurusd_lots().notional(Decimal("0.5"), Decimal("1.0850")) == Money.of(
            "54250.000", USD
        )

    def test_notional_uses_absolute_size(self) -> None:
        instrument = eurusd()
        assert instrument.notional(Decimal(-10_000), Decimal("1.0850")).is_positive

    def test_pip_value_of_a_standard_lot(self) -> None:
        # 100000 EUR at a 0.0001 pip is 10 USD per pip, the classic desk number.
        assert eurusd().value_per_pip(Decimal(100_000)) == Money.of(10, USD)

    def test_pip_value_of_a_yen_pair(self) -> None:
        # 100000 USD at a 0.01 pip is 1000 JPY per pip.
        assert usdjpy().value_per_pip(Decimal(100_000)) == Money.of(1000, JPY)

    def test_pip_value_via_lot_sizing(self) -> None:
        assert eurusd_lots().value_per_pip(Decimal(1)) == Money.of(10, USD)

    def test_tick_value(self) -> None:
        assert eurusd().value_per_tick(Decimal(100_000)) == Money.of(1, USD)
        assert btcusdt().value_per_tick(Decimal(1)) == Money.of("0.01", USDT)

    def test_pip_helpers_reject_instruments_without_a_pip(self) -> None:
        with pytest.raises(PipNotDefinedError, match="value_per_tick"):
            btcusdt().value_per_pip(Decimal(1))
        with pytest.raises(PipNotDefinedError, match="ticks"):
            btcusdt().pips_between(Decimal(1), Decimal(2))

    def test_pips_between_is_signed(self) -> None:
        assert eurusd().pips_between(Decimal("1.0850"), Decimal("1.0875")) == Decimal(25)
        assert eurusd().pips_between(Decimal("1.0875"), Decimal("1.0850")) == Decimal(-25)


class TestMargin:
    def test_margin_defaults_to_the_venue_maximum_leverage(self) -> None:
        # 10000 EUR at 1.0850 is 10850 USD notional; at 30x that is 361.666... USD.
        margin = eurusd().required_margin(Decimal(10_000), Decimal("1.0850"))
        assert margin.quantize() == Money.of("361.67", USD)

    def test_explicit_leverage_below_the_cap(self) -> None:
        margin = eurusd().required_margin(Decimal(10_000), Decimal("1.0850"), leverage=10)
        assert margin == Money.of("1085.0", USD)

    def test_leverage_above_the_cap_is_refused(self) -> None:
        with pytest.raises(InvalidQuantityError, match="exceeds the venue maximum"):
            eurusd().required_margin(Decimal(10_000), Decimal("1.0850"), leverage=100)

    def test_non_positive_leverage_is_refused(self) -> None:
        with pytest.raises(InvalidQuantityError, match="leverage must be positive"):
            eurusd().required_margin(Decimal(10_000), Decimal("1.0850"), leverage=0)

    def test_margin_is_undefined_without_leverage(self) -> None:
        with pytest.raises(InstrumentDefinitionError, match="no max_leverage"):
            btcusdt().required_margin(Decimal(1), Decimal(60_000))

    def test_explicit_leverage_works_without_a_cap(self) -> None:
        margin = btcusdt().required_margin(Decimal(1), Decimal(60_000), leverage=2)
        assert margin == Money.of(30_000, USDT)


class TestStatusAndHours:
    def test_active_instrument_inside_a_session_is_open(self) -> None:
        instant = datetime(2025, 3, 5, 12, 0, tzinfo=UTC)
        assert eurusd().is_open(instant)

    def test_active_instrument_outside_a_session_is_closed(self) -> None:
        assert not eurusd().is_open(datetime(2025, 3, 8, 12, 0, tzinfo=UTC))

    def test_halted_instrument_is_closed_even_during_a_session(self) -> None:
        halted = replace(eurusd(), status=InstrumentStatus.HALTED)
        assert not halted.is_open(datetime(2025, 3, 5, 12, 0, tzinfo=UTC))
        assert not halted.is_tradeable

    def test_delisted_instrument_is_not_tradeable(self) -> None:
        assert not replace(eurusd(), status=InstrumentStatus.DELISTED).is_tradeable

    def test_crypto_is_open_at_the_weekend(self) -> None:
        assert btcusdt().is_open(datetime(2025, 3, 8, 12, 0, tzinfo=UTC))


class TestSerialisation:
    @pytest.mark.parametrize(
        "factory", [eurusd, usdjpy, eurusd_lots, btcusdt, btcusdt_perp], ids=lambda f: f.__name__
    )
    def test_round_trip(self, factory: Callable[[], Instrument]) -> None:
        instrument = factory()
        restored = Instrument.from_mapping(instrument.to_mapping(), default_registry)
        assert restored == instrument

    def test_mapping_is_json_safe(self) -> None:
        payload = json.loads(json.dumps(btcusdt_perp().to_mapping()))
        assert Instrument.from_mapping(payload, default_registry) == btcusdt_perp()

    def test_decimals_survive_as_strings(self) -> None:
        mapping = eurusd().to_mapping()
        assert mapping["price_increment"] == "0.00001"
        assert mapping["pip_size"] == "0.0001"


class TestCryptoAndForexCoexist:
    def test_the_same_pair_at_two_venues_sizes_differently(self) -> None:
        unit_venue, lot_venue = eurusd(), eurusd_lots()
        assert unit_venue.symbol == lot_venue.symbol
        assert unit_venue.id != lot_venue.id
        # One standard lot expressed in each venue's own units is the same exposure.
        assert unit_venue.to_base_units(Decimal(100_000)) == lot_venue.to_base_units(Decimal(1))
        assert unit_venue.value_per_pip(Decimal(100_000)) == lot_venue.value_per_pip(Decimal(1))

    def test_schedules_differ_by_asset_class(self) -> None:
        assert eurusd().schedule is FOREX_WEEK
        assert btcusdt().schedule is CRYPTO_WEEK
        assert not eurusd().schedule.always_open
        assert btcusdt().schedule.always_open

    def test_financing_conventions_differ_by_asset_class(self) -> None:
        assert eurusd().financing.rollover_time == time(17, 0)
        assert btcusdt_perp().financing.funding_interval is not None
        assert btcusdt().financing.rollover_time is None
