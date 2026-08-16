"""Turning Bybit's instrument metadata into :class:`~tradingsys.core.instrument.Instrument`.

Every sizing and pricing number here comes from ``GET /v5/market/instruments-info``.
None of it is written down in this file. A minimum order quantity that lives in source
is correct until the venue changes it, at which point orders are rejected by a rule
nothing in the repository mentions, and the fix is a code change and a deploy rather
than a restart.

Bybit describes the two categories we care about differently, and the difference is not
cosmetic:

``spot``
    Sized in the base asset with ``basePrecision`` as the step, floored by a *cash*
    minimum, ``minOrderAmt``, denominated in the quote asset. No financing.

``linear``
    A perpetual sized in the base asset with its own ``qtyStep``, floored by both a
    quantity minimum and ``minNotionalValue``, carrying leverage and paying funding
    every ``fundingInterval`` minutes.

So the same pair is a different instrument in each category, with different minimum
sizes, and mapping them through one code path with defaults filled in for whatever is
missing would quietly produce an instrument that is neither. Each category has its own
function, each requires exactly the fields its category publishes, and a payload
missing one of them raises rather than substituting a plausible number.

The funding anchor is not in the instruments response: only the interval is. The next
settlement instant comes from ``/v5/market/tickers`` as ``nextFundingTime``, so the
caller passes it in. Deriving it from the interval and midnight would be a guess that
happens to be right today.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from tradingsys.core.financing import FinancingSpec
from tradingsys.core.instrument import (
    AssetClass,
    Instrument,
    InstrumentId,
    InstrumentStatus,
    QuantityUnit,
)
from tradingsys.core.money import Money
from tradingsys.core.numeric import to_decimal
from tradingsys.core.schedule import TradingSchedule
from tradingsys.venues.errors import VenueResponseError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tradingsys.core.currency import CurrencyRegistry

__all__ = [
    "VENUE",
    "BybitCategory",
    "instrument_from_linear",
    "instrument_from_spot",
    "next_funding_time",
]

VENUE: Final = "bybit"

type BybitCategory = str
"""Bybit's product type, ``spot`` or ``linear``. Kept as the venue's own spelling."""

_STATUS: Final[Mapping[str, InstrumentStatus]] = {
    "Trading": InstrumentStatus.ACTIVE,
    "PreLaunch": InstrumentStatus.HALTED,
    "Delivering": InstrumentStatus.HALTED,
    "Closed": InstrumentStatus.DELISTED,
}
"""Bybit instrument status to ours.

Unmapped values raise. A status we have not seen before is not a reason to assume the
instrument is tradeable, which is what any default here would amount to.
"""

_CRYPTO_IS_ALWAYS_OPEN: Final = TradingSchedule.continuous()
"""Bybit runs 24/7 with no scheduled close, unlike the forex venue."""


def instrument_from_spot(payload: Mapping[str, Any], currencies: CurrencyRegistry) -> Instrument:
    """Build a spot instrument from one element of the ``instruments-info`` list.

    Args:
        payload: One object from ``result.list`` with ``category=spot``.
        currencies: Registry used to resolve ``baseCoin`` and ``quoteCoin``. An asset
            it does not know raises, rather than being invented with a guessed
            precision that would then round money.

    Raises:
        VenueResponseError: A field is missing, empty, or not a number.
        UnknownCurrencyError: The registry does not know the base or quote asset.
    """
    symbol = _text(payload, "symbol")
    base = currencies.get(_text(payload, "baseCoin"))
    quote = currencies.get(_text(payload, "quoteCoin"))
    lot = _section(payload, "lotSizeFilter", symbol)
    price = _section(payload, "priceFilter", symbol)

    tick_size = _decimal(price, "tickSize", symbol)
    return Instrument(
        id=InstrumentId(venue=VENUE, symbol=f"{base.code}/{quote.code}"),
        venue_symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        base_currency=base,
        quote_currency=quote,
        # Spot settles in the asset bought, but the account and every risk figure are
        # denominated in the quote, and the settlement currency is what the position
        # is marked in. Bybit's unified account holds the quote.
        settlement_currency=quote,
        price_increment=tick_size,
        price_precision=_places(tick_size),
        quantity_unit=QuantityUnit.UNITS,
        contract_size=Decimal(1),
        quantity_increment=_decimal(lot, "basePrecision", symbol),
        min_quantity=_decimal(lot, "minOrderQty", symbol),
        max_quantity=_decimal(lot, "maxOrderQty", symbol),
        # The binding floor on spot is cash, not quantity: 0.000001 BTC clears
        # minOrderQty and is rejected for being worth less than minOrderAmt.
        min_notional=Money(_decimal(lot, "minOrderAmt", symbol), quote),
        # Spot pays no funding. Borrowing on margin does cost interest, but that is a
        # property of a margin position rather than of the instrument, and this system
        # does not open one.
        financing=FinancingSpec.none(),
        schedule=_CRYPTO_IS_ALWAYS_OPEN,
        max_leverage=None,
        status=_status(payload, symbol),
    )


def instrument_from_linear(
    payload: Mapping[str, Any],
    currencies: CurrencyRegistry,
    *,
    funding_anchor: datetime,
) -> Instrument:
    """Build a linear perpetual from one element of the ``instruments-info`` list.

    Args:
        payload: One object from ``result.list`` with ``category=linear``.
        currencies: Registry used to resolve the base, quote, and settle assets.
        funding_anchor: A known funding settlement instant, from ``nextFundingTime`` on
            the tickers endpoint. Required rather than derived: the interval alone
            does not say where the cycle starts, and a wrong phase misprices every
            overnight hold in a backtest by a full funding payment.

    Raises:
        VenueResponseError: A field is missing, empty, not a number, or the contract type
            is not a perpetual.
        UnknownCurrencyError: The registry does not know one of the assets.
    """
    symbol = _text(payload, "symbol")
    contract_type = _text(payload, "contractType")
    if contract_type != "LinearPerpetual":
        raise VenueResponseError(
            VENUE,
            f"{symbol}: expected a LinearPerpetual, got {contract_type!r}. Dated futures "
            f"expire and settle, so they are a different asset class with a different "
            f"financing model, and mapping one here would model it as never expiring.",
        )
    base = currencies.get(_text(payload, "baseCoin"))
    quote = currencies.get(_text(payload, "quoteCoin"))
    settle = currencies.get(_text(payload, "settleCoin"))
    lot = _section(payload, "lotSizeFilter", symbol)
    price = _section(payload, "priceFilter", symbol)
    leverage = _section(payload, "leverageFilter", symbol)

    tick_size = _decimal(price, "tickSize", symbol)
    interval_minutes = _int(payload, "fundingInterval", symbol)
    return Instrument(
        id=InstrumentId(venue=VENUE, symbol=f"{base.code}/{quote.code}"),
        venue_symbol=symbol,
        asset_class=AssetClass.CRYPTO_PERPETUAL,
        base_currency=base,
        quote_currency=quote,
        settlement_currency=settle,
        price_increment=tick_size,
        # priceScale is the venue's own display precision and is at least as wide as
        # the tick, so it is taken as published rather than inferred from the tick.
        price_precision=_int(payload, "priceScale", symbol),
        quantity_unit=QuantityUnit.UNITS,
        contract_size=Decimal(1),
        quantity_increment=_decimal(lot, "qtyStep", symbol),
        min_quantity=_decimal(lot, "minOrderQty", symbol),
        max_quantity=_decimal(lot, "maxOrderQty", symbol),
        min_notional=Money(_decimal(lot, "minNotionalValue", symbol), quote),
        financing=FinancingSpec.funding(
            interval=timedelta(minutes=interval_minutes), anchor=funding_anchor
        ),
        schedule=_CRYPTO_IS_ALWAYS_OPEN,
        max_leverage=_decimal(leverage, "maxLeverage", symbol),
        status=_status(payload, symbol),
    )


def next_funding_time(payload: Mapping[str, Any]) -> datetime:
    """The next funding settlement, from one element of the tickers response.

    Bybit reports it as milliseconds since the epoch, as a string.

    Raises:
        VenueResponseError: The field is absent, not an integer, or not a plausible instant.
    """
    symbol = _text(payload, "symbol")
    raw = _text(payload, "nextFundingTime")
    try:
        milliseconds = int(raw)
    except ValueError:
        raise VenueResponseError(
            VENUE, f"{symbol}: nextFundingTime {raw!r} is not an integer number of milliseconds"
        ) from None
    if milliseconds <= 0:
        raise VenueResponseError(
            VENUE,
            f"{symbol}: nextFundingTime is {milliseconds}, which is not an instant. Bybit "
            f"reports 0 for products that do not fund, and a perpetual always does.",
        )
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


def _status(payload: Mapping[str, Any], symbol: str) -> InstrumentStatus:
    raw = _text(payload, "status")
    try:
        return _STATUS[raw]
    except KeyError:
        raise VenueResponseError(
            VENUE,
            f"{symbol}: unknown Bybit status {raw!r}. Known values are "
            f"{sorted(_STATUS)}; treating an unrecognised one as tradeable would let "
            f"the system send orders into a state nobody has looked at.",
        ) from None


def _section(payload: Mapping[str, Any], name: str, symbol: str) -> Mapping[str, Any]:
    section = payload.get(name)
    if not isinstance(section, dict):
        raise VenueResponseError(VENUE, f"{symbol}: {name} is missing or is not an object")
    return section


def _text(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise VenueResponseError(
            VENUE, f"{name} is missing or empty in the Bybit payload: {value!r}"
        )
    return value


def _decimal(section: Mapping[str, Any], name: str, symbol: str) -> Decimal:
    value = section.get(name)
    if not isinstance(value, str) or not value:
        raise VenueResponseError(VENUE, f"{symbol}: {name} is missing or empty, got {value!r}")
    # Bybit sends every number as a decimal string, which is the one representation
    # that survives the trip exactly. Parsing it as a float first and converting after
    # would put a binary approximation between the venue and the database.
    return to_decimal(value, what=f"{symbol} {name}")


def _int(payload: Mapping[str, Any], name: str, symbol: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise VenueResponseError(
            VENUE, f"{symbol}: {name} is missing or is not a number, got {value!r}"
        )
    try:
        return int(value)
    except ValueError:
        raise VenueResponseError(
            VENUE, f"{symbol}: {name} is not an integer, got {value!r}"
        ) from None


def _places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    if not isinstance(exponent, int):  # pragma: no cover - guarded by to_decimal
        raise VenueResponseError(VENUE, f"{value} is not a finite decimal")
    return max(0, -exponent)
