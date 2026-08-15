"""Realistic domain objects for tests.

These are real instrument definitions with the conventions the corresponding venues
actually use, not simplified stand-ins. Using genuine tick sizes, pip sizes, and
contract sizes is what makes the arithmetic tests meaningful.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from tradingsys.core.currency import default_registry
from tradingsys.core.financing import FinancingSpec
from tradingsys.core.instrument import (
    AssetClass,
    Instrument,
    InstrumentId,
    QuantityUnit,
)
from tradingsys.core.money import Money
from tradingsys.core.schedule import TradingSchedule, Weekday, WeeklySession

USD = default_registry.get("USD")
EUR = default_registry.get("EUR")
JPY = default_registry.get("JPY")
BTC = default_registry.get("BTC")
USDT = default_registry.get("USDT")

FOREX_WEEK = TradingSchedule.weekly(
    timezone="America/New_York",
    sessions=[
        WeeklySession(
            open_day=Weekday.SUNDAY,
            open_time=time(17, 0),
            close_day=Weekday.FRIDAY,
            close_time=time(17, 0),
        )
    ],
)
"""The spot forex week: opens 17:00 New York on Sunday, closes 17:00 New York Friday."""

CRYPTO_WEEK = TradingSchedule.continuous()


def eurusd() -> Instrument:
    """EUR/USD as a unit sized forex broker lists it: 1e-5 tick, 1e-4 pip."""
    return Instrument(
        id=InstrumentId(venue="fxbroker", symbol="EUR/USD"),
        venue_symbol="EUR_USD",
        asset_class=AssetClass.FX_SPOT,
        base_currency=EUR,
        quote_currency=USD,
        settlement_currency=USD,
        price_increment=Decimal("0.00001"),
        price_precision=5,
        pip_size=Decimal("0.0001"),
        quantity_unit=QuantityUnit.UNITS,
        contract_size=Decimal(1),
        quantity_increment=Decimal(1),
        min_quantity=Decimal(1),
        max_quantity=Decimal(100_000_000),
        max_leverage=Decimal(30),
        financing=FinancingSpec.swap(
            rollover_time=time(17, 0), triple_rollover_day=Weekday.WEDNESDAY
        ),
        schedule=FOREX_WEEK,
    )


def usdjpy() -> Instrument:
    """USD/JPY, where the pip is 0.01 rather than 0.0001."""
    return Instrument(
        id=InstrumentId(venue="fxbroker", symbol="USD/JPY"),
        venue_symbol="USD_JPY",
        asset_class=AssetClass.FX_SPOT,
        base_currency=USD,
        quote_currency=JPY,
        settlement_currency=JPY,
        price_increment=Decimal("0.001"),
        price_precision=3,
        pip_size=Decimal("0.01"),
        quantity_unit=QuantityUnit.UNITS,
        contract_size=Decimal(1),
        quantity_increment=Decimal(1),
        min_quantity=Decimal(1),
        max_leverage=Decimal(30),
        financing=FinancingSpec.swap(rollover_time=time(17, 0)),
        schedule=FOREX_WEEK,
    )


def eurusd_lots() -> Instrument:
    """The same pair at a lot sized venue: 0.01 lot steps of a 100000 unit contract."""
    return Instrument(
        id=InstrumentId(venue="lotbroker", symbol="EUR/USD"),
        venue_symbol="EURUSD",
        asset_class=AssetClass.FX_SPOT,
        base_currency=EUR,
        quote_currency=USD,
        settlement_currency=USD,
        price_increment=Decimal("0.00001"),
        price_precision=5,
        pip_size=Decimal("0.0001"),
        quantity_unit=QuantityUnit.LOTS,
        contract_size=Decimal(100_000),
        quantity_increment=Decimal("0.01"),
        min_quantity=Decimal("0.01"),
        max_quantity=Decimal(100),
        max_leverage=Decimal(30),
        financing=FinancingSpec.swap(rollover_time=time(0, 0)),
        schedule=FOREX_WEEK,
    )


def btcusdt() -> Instrument:
    """BTC/USDT spot: no pip convention, no leverage, no carry."""
    return Instrument(
        id=InstrumentId(venue="cryptoex", symbol="BTC/USDT"),
        venue_symbol="BTCUSDT",
        asset_class=AssetClass.CRYPTO_SPOT,
        base_currency=BTC,
        quote_currency=USDT,
        settlement_currency=USDT,
        price_increment=Decimal("0.01"),
        price_precision=2,
        pip_size=None,
        quantity_unit=QuantityUnit.UNITS,
        contract_size=Decimal(1),
        quantity_increment=Decimal("0.00001"),
        min_quantity=Decimal("0.00001"),
        min_notional=Money.of(5, USDT),
        financing=FinancingSpec.none(),
        schedule=CRYPTO_WEEK,
    )


def btcusdt_perp() -> Instrument:
    """A BTC/USDT perpetual with eight hourly funding anchored to UTC midnight."""
    return Instrument(
        id=InstrumentId(venue="cryptoex", symbol="BTC/USDT-PERP"),
        venue_symbol="BTCUSDT_PERP",
        asset_class=AssetClass.CRYPTO_PERPETUAL,
        base_currency=BTC,
        quote_currency=USDT,
        settlement_currency=USDT,
        price_increment=Decimal("0.1"),
        price_precision=1,
        pip_size=None,
        quantity_unit=QuantityUnit.CONTRACTS,
        contract_size=Decimal("0.001"),
        quantity_increment=Decimal(1),
        min_quantity=Decimal(1),
        max_leverage=Decimal(20),
        financing=FinancingSpec.funding(
            interval=timedelta(hours=8),
            anchor=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        ),
        schedule=CRYPTO_WEEK,
    )
