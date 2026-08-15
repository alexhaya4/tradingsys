"""Instrument definitions.

An :class:`Instrument` is everything the system needs to know to size, price, and cost
a position in one tradeable symbol at one venue. It deliberately spans both venue
families we target, which forces a few modelling choices:

*Sizing.* Forex brokers on the OANDA model size in units of the base currency and
accept fractional sizes; retail platforms and most crypto derivatives size in lots or
contracts with a fixed contract size. :attr:`Instrument.quantity_unit` names which
convention applies and :attr:`Instrument.contract_size` converts between the two, so
risk code can always reason in base units via :meth:`Instrument.to_base_units`.

*Price granularity.* Tick size and pip size are different things. EUR/USD ticks in
0.00001 but a pip is 0.0001; USD/JPY ticks in 0.001 with a 0.01 pip; BTC/USDT ticks in
0.01 and has no pip convention at all. Both are stored, and pip denominated helpers
raise rather than inventing a pip for instruments that have none.

*Venue scoping.* The same symbol has different tick sizes, minimums, and leverage caps
at different venues, so an instrument is identified by :class:`InstrumentId`, the pair
of venue and symbol, and never by symbol alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self, final

from tradingsys.core.currency import Currency, CurrencyRegistry
from tradingsys.core.errors import (
    InstrumentDefinitionError,
    InvalidPriceError,
    InvalidQuantityError,
    PipNotDefinedError,
)
from tradingsys.core.financing import FinancingSpec
from tradingsys.core.money import Money
from tradingsys.core.numeric import Numeric, exact_context, to_decimal
from tradingsys.core.rounding import Rounding
from tradingsys.core.schedule import TradingSchedule

__all__ = [
    "AssetClass",
    "Instrument",
    "InstrumentId",
    "InstrumentStatus",
    "QuantityUnit",
]


class AssetClass(StrEnum):
    """What kind of thing is being traded, which determines settlement semantics."""

    FX_SPOT = "fx_spot"
    METAL_SPOT = "metal_spot"
    CRYPTO_SPOT = "crypto_spot"
    CRYPTO_PERPETUAL = "crypto_perpetual"
    CRYPTO_FUTURE = "crypto_future"


class QuantityUnit(StrEnum):
    """The unit in which order quantities are expressed at a venue."""

    UNITS = "units"
    """One quantity step is one unit of the base currency or asset."""

    LOTS = "lots"
    """One quantity step is one lot of :attr:`Instrument.contract_size` base units."""

    CONTRACTS = "contracts"
    """One quantity step is one contract of :attr:`Instrument.contract_size` base units."""


class InstrumentStatus(StrEnum):
    """Whether the instrument can currently be traded, independent of session hours."""

    ACTIVE = "active"
    HALTED = "halted"
    DELISTED = "delisted"


@final
@dataclass(frozen=True, slots=True, order=True)
class InstrumentId:
    """Venue scoped identity of an instrument.

    Attributes:
        venue: Stable identifier of the venue the definition came from.
        symbol: Canonical, venue-neutral symbol such as ``EUR/USD`` or ``BTC/USDT``.
            Adapters translate to and from their venue's own spelling; the venue's
            format never appears here.
    """

    venue: str
    symbol: str

    def __post_init__(self) -> None:
        if not self.venue or self.venue.strip() != self.venue:
            raise InstrumentDefinitionError(f"venue must be non-empty and unpadded: {self.venue!r}")
        if not self.symbol or self.symbol.strip() != self.symbol:
            raise InstrumentDefinitionError(
                f"symbol must be non-empty and unpadded: {self.symbol!r}"
            )

    def __str__(self) -> str:
        return f"{self.venue}:{self.symbol}"


@final
@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradeable symbol at one venue, with its sizing, pricing, and cost conventions."""

    id: InstrumentId
    venue_symbol: str
    """The venue's own spelling of this instrument.

    Kept beside the canonical symbol rather than derived from it, because the
    transformation is not a rule: one venue writes ``EUR_USD``, another ``EURUSD``,
    another ``EUR/USD``, and a fourth uses an opaque numeric identifier. Adapters map
    in both directions using this field, which is why it is required rather than
    defaulted to the canonical form.
    """
    asset_class: AssetClass
    base_currency: Currency
    quote_currency: Currency
    settlement_currency: Currency
    price_increment: Decimal
    price_precision: int
    quantity_unit: QuantityUnit
    contract_size: Decimal
    quantity_increment: Decimal
    min_quantity: Decimal
    financing: FinancingSpec
    schedule: TradingSchedule
    pip_size: Decimal | None = None
    max_quantity: Decimal | None = None
    min_notional: Money | None = None
    max_leverage: Decimal | None = None
    status: InstrumentStatus = InstrumentStatus.ACTIVE

    def __post_init__(self) -> None:  # noqa: PLR0912 - one branch per invariant, kept flat
        name = str(self.id)
        if not self.venue_symbol or self.venue_symbol.strip() != self.venue_symbol:
            raise InstrumentDefinitionError(
                f"{name}: venue_symbol must be non-empty and unpadded, got {self.venue_symbol!r}"
            )
        if self.base_currency == self.quote_currency:
            raise InstrumentDefinitionError(
                f"{name}: base and quote currency are both {self.base_currency.code}"
            )
        if self.settlement_currency not in (self.base_currency, self.quote_currency):
            raise InstrumentDefinitionError(
                f"{name}: settlement currency {self.settlement_currency.code} must be either "
                f"the base ({self.base_currency.code}) or the quote ({self.quote_currency.code})"
            )
        if self.price_increment <= 0:
            raise InstrumentDefinitionError(
                f"{name}: price_increment must be positive, got {self.price_increment}"
            )
        if self.price_precision < 0:
            raise InstrumentDefinitionError(
                f"{name}: price_precision must not be negative, got {self.price_precision}"
            )
        if _decimal_places(self.price_increment) > self.price_precision:
            raise InstrumentDefinitionError(
                f"{name}: price_increment {self.price_increment} has more decimal places than "
                f"price_precision {self.price_precision} allows"
            )
        if self.pip_size is not None:
            if self.pip_size <= 0:
                raise InstrumentDefinitionError(
                    f"{name}: pip_size must be positive, got {self.pip_size}"
                )
            if not _is_multiple(self.pip_size, self.price_increment):
                raise InstrumentDefinitionError(
                    f"{name}: pip_size {self.pip_size} is not a whole multiple of "
                    f"price_increment {self.price_increment}"
                )
        if self.contract_size <= 0:
            raise InstrumentDefinitionError(
                f"{name}: contract_size must be positive, got {self.contract_size}"
            )
        if self.quantity_unit is QuantityUnit.UNITS and self.contract_size != 1:
            raise InstrumentDefinitionError(
                f"{name}: unit sized instruments must have contract_size 1, got "
                f"{self.contract_size}"
            )
        if self.quantity_increment <= 0:
            raise InstrumentDefinitionError(
                f"{name}: quantity_increment must be positive, got {self.quantity_increment}"
            )
        if self.min_quantity <= 0:
            raise InstrumentDefinitionError(
                f"{name}: min_quantity must be positive, got {self.min_quantity}"
            )
        if not _is_multiple(self.min_quantity, self.quantity_increment):
            raise InstrumentDefinitionError(
                f"{name}: min_quantity {self.min_quantity} is not a whole multiple of "
                f"quantity_increment {self.quantity_increment}"
            )
        if self.max_quantity is not None:
            if self.max_quantity < self.min_quantity:
                raise InstrumentDefinitionError(
                    f"{name}: max_quantity {self.max_quantity} is below min_quantity "
                    f"{self.min_quantity}"
                )
            if not _is_multiple(self.max_quantity, self.quantity_increment):
                raise InstrumentDefinitionError(
                    f"{name}: max_quantity {self.max_quantity} is not a whole multiple of "
                    f"quantity_increment {self.quantity_increment}"
                )
        if self.min_notional is not None and self.min_notional.currency != self.quote_currency:
            raise InstrumentDefinitionError(
                f"{name}: min_notional is denominated in {self.min_notional.currency.code} but "
                f"notional is quoted in {self.quote_currency.code}"
            )
        if self.max_leverage is not None and self.max_leverage <= 0:
            raise InstrumentDefinitionError(
                f"{name}: max_leverage must be positive, got {self.max_leverage}"
            )

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------

    @property
    def venue(self) -> str:
        return self.id.venue

    @property
    def symbol(self) -> str:
        return self.id.symbol

    @property
    def is_tradeable(self) -> bool:
        """Whether the venue currently accepts orders, ignoring session hours."""
        return self.status is InstrumentStatus.ACTIVE

    def is_open(self, instant: datetime) -> bool:
        """Whether the instrument is both active and inside a trading session."""
        return self.is_tradeable and self.schedule.is_open(instant)

    # ------------------------------------------------------------------
    # quantity handling
    # ------------------------------------------------------------------

    def to_base_units(self, quantity: Numeric) -> Decimal:
        """Convert a venue quantity into units of the base asset.

        For a unit sized forex instrument this is the identity. For a lot sized one it
        multiplies by the contract size, so 0.5 lots of a 100000 unit contract becomes
        50000 base units.
        """
        value = to_decimal(quantity, what="quantity")
        with exact_context():
            return value * self.contract_size

    def from_base_units(self, base_units: Numeric) -> Decimal:
        """Convert units of the base asset into a venue quantity."""
        value = to_decimal(base_units, what="base_units")
        with exact_context():
            return value / self.contract_size

    def quantize_quantity(self, quantity: Numeric, rounding: Rounding = Rounding.DOWN) -> Decimal:
        """Snap a quantity to the venue's step size.

        Rounds down by default: overshooting a step boundary upward can breach a
        maximum size or an available margin limit, while undershooting only trades
        slightly less than intended.
        """
        value = to_decimal(quantity, what="quantity")
        with exact_context():
            steps = (value / self.quantity_increment).quantize(Decimal(1), rounding=rounding.value)
            return steps * self.quantity_increment

    def is_valid_quantity(self, quantity: Numeric) -> bool:
        """Whether the venue would accept this quantity, ignoring notional minimums."""
        try:
            self.validate_quantity(quantity)
        except InvalidQuantityError:
            return False
        return True

    def validate_quantity(self, quantity: Numeric) -> Decimal:
        """Return the quantity if the venue would accept it, otherwise raise.

        Sign is ignored: direction is carried by the order side, not by the magnitude,
        so the absolute value is checked against the venue's limits.

        Raises:
            InvalidQuantityError: The quantity is zero, below the minimum, above the
                maximum, or off the step grid.
        """
        value = to_decimal(quantity, what="quantity")
        magnitude = abs(value)
        if magnitude == 0:
            raise InvalidQuantityError(f"{self.id}: quantity must not be zero")
        if magnitude < self.min_quantity:
            raise InvalidQuantityError(
                f"{self.id}: quantity {magnitude} is below the minimum {self.min_quantity} "
                f"{self.quantity_unit.value}"
            )
        if self.max_quantity is not None and magnitude > self.max_quantity:
            raise InvalidQuantityError(
                f"{self.id}: quantity {magnitude} exceeds the maximum {self.max_quantity} "
                f"{self.quantity_unit.value}"
            )
        if not _is_multiple(magnitude, self.quantity_increment):
            raise InvalidQuantityError(
                f"{self.id}: quantity {magnitude} is not a whole multiple of the step size "
                f"{self.quantity_increment}"
            )
        return value

    # ------------------------------------------------------------------
    # price handling
    # ------------------------------------------------------------------

    def quantize_price(self, price: Numeric, rounding: Rounding = Rounding.HALF_EVEN) -> Decimal:
        """Snap a price to the venue's tick size."""
        value = to_decimal(price, what="price")
        with exact_context():
            ticks = (value / self.price_increment).quantize(Decimal(1), rounding=rounding.value)
            return (ticks * self.price_increment).quantize(Decimal(1).scaleb(-self.price_precision))

    def validate_price(self, price: Numeric) -> Decimal:
        """Return the price if it sits on the tick grid, otherwise raise.

        Raises:
            InvalidPriceError: The price is not positive or is off the tick grid.
        """
        value = to_decimal(price, what="price")
        if value <= 0:
            raise InvalidPriceError(f"{self.id}: price must be positive, got {value}")
        if not _is_multiple(value, self.price_increment):
            raise InvalidPriceError(
                f"{self.id}: price {value} is not a whole multiple of the tick size "
                f"{self.price_increment}"
            )
        return value

    # ------------------------------------------------------------------
    # valuation
    # ------------------------------------------------------------------

    def notional(self, quantity: Numeric, price: Numeric) -> Money:
        """Position value in the quote currency.

        Uses the absolute quantity, so the result is a size rather than a signed
        exposure.
        """
        units = abs(self.to_base_units(quantity))
        price_value = to_decimal(price, what="price")
        with exact_context():
            return Money(units * price_value, self.quote_currency)

    def value_per_tick(self, quantity: Numeric) -> Money:
        """Profit or loss in the quote currency for a one tick move."""
        units = abs(self.to_base_units(quantity))
        with exact_context():
            return Money(units * self.price_increment, self.quote_currency)

    def value_per_pip(self, quantity: Numeric) -> Money:
        """Profit or loss in the quote currency for a one pip move.

        Raises:
            PipNotDefinedError: The instrument has no pip convention.
        """
        if self.pip_size is None:
            raise PipNotDefinedError(
                f"{self.id}: no pip convention is defined for this instrument; use "
                f"value_per_tick and price_increment instead"
            )
        units = abs(self.to_base_units(quantity))
        with exact_context():
            return Money(units * self.pip_size, self.quote_currency)

    def pips_between(self, from_price: Numeric, to_price: Numeric) -> Decimal:
        """Signed distance between two prices measured in pips.

        Raises:
            PipNotDefinedError: The instrument has no pip convention.
        """
        if self.pip_size is None:
            raise PipNotDefinedError(
                f"{self.id}: no pip convention is defined for this instrument; measure "
                f"distance in ticks instead"
            )
        start = to_decimal(from_price, what="from_price")
        finish = to_decimal(to_price, what="to_price")
        with exact_context():
            return (finish - start) / self.pip_size

    def required_margin(
        self, quantity: Numeric, price: Numeric, leverage: Numeric | None = None
    ) -> Money:
        """Initial margin for a position, in the quote currency.

        Args:
            quantity: Size in the instrument's quantity unit.
            price: Price at which the position is opened.
            leverage: Effective leverage to apply. Defaults to the instrument's
                maximum, which yields the smallest margin the venue permits. Trading
                below the cap is the caller's choice and is passed explicitly.

        Raises:
            InstrumentDefinitionError: No leverage was supplied and the instrument has
                no maximum, so margin is undefined.
            InvalidQuantityError: The requested leverage exceeds the instrument's cap.
        """
        effective = self.max_leverage if leverage is None else to_decimal(leverage, what="leverage")
        if effective is None:
            raise InstrumentDefinitionError(
                f"{self.id}: cannot compute margin because the instrument has no "
                f"max_leverage and no leverage was supplied"
            )
        if effective <= 0:
            raise InvalidQuantityError(f"{self.id}: leverage must be positive, got {effective}")
        if self.max_leverage is not None and effective > self.max_leverage:
            raise InvalidQuantityError(
                f"{self.id}: leverage {effective} exceeds the venue maximum {self.max_leverage}"
            )
        return self.notional(quantity, price) / effective

    # ------------------------------------------------------------------
    # serialisation
    # ------------------------------------------------------------------

    def to_mapping(self) -> dict[str, Any]:
        """Serialise the parts of the definition that vary by venue.

        Currencies are emitted as codes; the reader resolves them against a
        :class:`~tradingsys.core.currency.CurrencyRegistry`.
        """
        return {
            "venue": self.id.venue,
            "symbol": self.id.symbol,
            "venue_symbol": self.venue_symbol,
            "asset_class": self.asset_class.value,
            "base_currency": self.base_currency.code,
            "quote_currency": self.quote_currency.code,
            "settlement_currency": self.settlement_currency.code,
            "price_increment": str(self.price_increment),
            "price_precision": self.price_precision,
            "pip_size": str(self.pip_size) if self.pip_size is not None else None,
            "quantity_unit": self.quantity_unit.value,
            "contract_size": str(self.contract_size),
            "quantity_increment": str(self.quantity_increment),
            "min_quantity": str(self.min_quantity),
            "max_quantity": str(self.max_quantity) if self.max_quantity is not None else None,
            "min_notional": (
                str(self.min_notional.amount) if self.min_notional is not None else None
            ),
            "max_leverage": str(self.max_leverage) if self.max_leverage is not None else None,
            "financing": self.financing.to_mapping(),
            "schedule": self.schedule.to_mapping(),
            "status": self.status.value,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], currencies: CurrencyRegistry) -> Self:
        """Rebuild an instrument from :meth:`to_mapping` output.

        Args:
            data: A mapping produced by :meth:`to_mapping`.
            currencies: Registry used to resolve the serialised currency codes.
        """
        quote = currencies.get(data["quote_currency"])
        min_notional_raw = data.get("min_notional")
        return cls(
            id=InstrumentId(venue=data["venue"], symbol=data["symbol"]),
            venue_symbol=str(data["venue_symbol"]),
            asset_class=AssetClass(data["asset_class"]),
            base_currency=currencies.get(data["base_currency"]),
            quote_currency=quote,
            settlement_currency=currencies.get(data["settlement_currency"]),
            price_increment=to_decimal(data["price_increment"], what="price_increment"),
            price_precision=int(data["price_precision"]),
            pip_size=_optional_decimal(data.get("pip_size"), "pip_size"),
            quantity_unit=QuantityUnit(data["quantity_unit"]),
            contract_size=to_decimal(data["contract_size"], what="contract_size"),
            quantity_increment=to_decimal(data["quantity_increment"], what="quantity_increment"),
            min_quantity=to_decimal(data["min_quantity"], what="min_quantity"),
            max_quantity=_optional_decimal(data.get("max_quantity"), "max_quantity"),
            min_notional=(
                Money(to_decimal(min_notional_raw, what="min_notional"), quote)
                if min_notional_raw is not None
                else None
            ),
            max_leverage=_optional_decimal(data.get("max_leverage"), "max_leverage"),
            financing=FinancingSpec.from_mapping(data["financing"]),
            schedule=TradingSchedule.from_mapping(data["schedule"]),
            status=InstrumentStatus(data.get("status", InstrumentStatus.ACTIVE.value)),
        )


def _optional_decimal(value: object, what: str) -> Decimal | None:
    return None if value is None else to_decimal(value, what=what)


def _decimal_places(value: Decimal) -> int:
    """Number of digits after the decimal point, never negative."""
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):  # NaN or infinity, excluded upstream by to_decimal
        raise InstrumentDefinitionError(f"cannot count decimal places of {value}")
    return max(0, -exponent)


def _is_multiple(value: Decimal, step: Decimal) -> bool:
    """Whether ``value`` is a whole multiple of ``step``, computed exactly."""
    with exact_context():
        return value % step == 0
