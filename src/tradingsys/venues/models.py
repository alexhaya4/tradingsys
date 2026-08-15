"""Value objects exchanged with venues.

Every type here is frozen, validated on construction, and expressed in the system's own
vocabulary rather than any venue's. Prices and sizes are :class:`~decimal.Decimal`;
anything denominated in a currency is :class:`~tradingsys.core.money.Money`.

Timestamps are timezone aware and normalised to UTC on construction, so no downstream
comparison can be wrong by an offset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Self, final

from tradingsys.core.clock import ensure_utc
from tradingsys.core.errors import DomainError
from tradingsys.core.money import Money
from tradingsys.core.numeric import exact_context, to_decimal
from tradingsys.venues.enums import (
    CandlePrice,
    Granularity,
    LiquidityRole,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionMode,
    PositionSide,
    TimeInForce,
    TriggerCondition,
)
from tradingsys.venues.errors import UnsupportedVenueOperationError

if TYPE_CHECKING:
    from tradingsys.core.instrument import Instrument, InstrumentId

__all__ = [
    "AccountSnapshot",
    "BookLevel",
    "Candle",
    "ExecutionEvent",
    "Fill",
    "FundingRate",
    "Order",
    "OrderBook",
    "OrderRequest",
    "Position",
    "Quote",
    "SwapRates",
    "Trade",
    "VenueCapabilities",
]


# ----------------------------------------------------------------------------------
# market data
# ----------------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class Quote:
    """A top of book bid and ask at one instant.

    This is the unit of a price stream for both venue families: forex brokers publish
    exactly this, and a crypto exchange's book top reduces to it.
    """

    instrument_id: InstrumentId
    ts: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    tradeable: bool = True
    """False when the venue is publishing an indicative price it will not trade on,
    which happens around session boundaries and during halts."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, what="quote timestamp"))
        for name in ("bid", "ask"):
            value = to_decimal(getattr(self, name), what=name)
            if value <= 0:
                raise DomainError(f"{self.instrument_id}: {name} must be positive, got {value}")
            object.__setattr__(self, name, value)
        for name in ("bid_size", "ask_size"):
            raw = getattr(self, name)
            if raw is None:
                continue
            value = to_decimal(raw, what=name)
            if value < 0:
                raise DomainError(f"{self.instrument_id}: {name} must not be negative")
            object.__setattr__(self, name, value)
        if self.bid > self.ask:
            raise DomainError(
                f"{self.instrument_id}: crossed quote, bid {self.bid} is above ask {self.ask}"
            )

    @property
    def mid(self) -> Decimal:
        with exact_context():
            return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def spread_in_pips(self, instrument: Instrument) -> Decimal:
        """Spread measured in the instrument's pips.

        Raises:
            PipNotDefinedError: The instrument has no pip convention.
        """
        return instrument.pips_between(self.bid, self.ask)

    def price_for(self, side: OrderSide) -> Decimal:
        """The price a taker of ``side`` would cross to."""
        return self.ask if side is OrderSide.BUY else self.bid


@final
@dataclass(frozen=True, slots=True)
class Trade:
    """A single executed trade published by a venue's public tape."""

    instrument_id: InstrumentId
    ts: datetime
    price: Decimal
    size: Decimal
    aggressor: OrderSide | None = None
    """Side that crossed the spread, when the venue reports it."""
    trade_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, what="trade timestamp"))
        price = to_decimal(self.price, what="price")
        size = to_decimal(self.size, what="size")
        if price <= 0:
            raise DomainError(f"{self.instrument_id}: trade price must be positive, got {price}")
        if size <= 0:
            raise DomainError(f"{self.instrument_id}: trade size must be positive, got {size}")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "size", size)


@final
@dataclass(frozen=True, slots=True)
class BookLevel:
    """One price level of an order book."""

    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        price = to_decimal(self.price, what="price")
        size = to_decimal(self.size, what="size")
        if price <= 0:
            raise DomainError(f"book level price must be positive, got {price}")
        if size <= 0:
            raise DomainError(f"book level size must be positive, got {size}")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "size", size)


@final
@dataclass(frozen=True, slots=True)
class OrderBook:
    """A depth snapshot, ordered best price first on each side."""

    instrument_id: InstrumentId
    ts: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, what="book timestamp"))
        bids = [level.price for level in self.bids]
        asks = [level.price for level in self.asks]
        if bids != sorted(bids, reverse=True):
            raise DomainError(f"{self.instrument_id}: bids must be ordered highest price first")
        if asks != sorted(asks):
            raise DomainError(f"{self.instrument_id}: asks must be ordered lowest price first")
        if bids and asks and bids[0] > asks[0]:
            raise DomainError(
                f"{self.instrument_id}: crossed book, best bid {bids[0]} is above best ask "
                f"{asks[0]}"
            )

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    def to_quote(self) -> Quote | None:
        """The top of book as a :class:`Quote`, or ``None`` if either side is empty."""
        if not self.bids or not self.asks:
            return None
        return Quote(
            instrument_id=self.instrument_id,
            ts=self.ts,
            bid=self.bids[0].price,
            ask=self.asks[0].price,
            bid_size=self.bids[0].size,
            ask_size=self.asks[0].size,
        )


@final
@dataclass(frozen=True, slots=True)
class Candle:
    """One aggregated bar.

    ``start`` is the inclusive left edge of the bar's interval, so a bar's interval is
    ``[start, start + granularity.duration)``. Storing the left edge rather than the
    right removes the off-by-one-bar error that appears when two systems disagree
    about which convention a timestamp follows.
    """

    instrument_id: InstrumentId
    granularity: Granularity
    price: CandlePrice
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(0)
    trade_count: int = 0
    complete: bool = True
    """False for the bar currently forming. Incomplete bars must never be stored as
    final or used to trigger a decision that assumes a closed bar."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_utc(self.start, what="candle start"))
        for name in ("open", "high", "low", "close"):
            value = to_decimal(getattr(self, name), what=name)
            if value <= 0:
                raise DomainError(f"{self.instrument_id}: candle {name} must be positive")
            object.__setattr__(self, name, value)
        volume = to_decimal(self.volume, what="volume")
        if volume < 0:
            raise DomainError(f"{self.instrument_id}: candle volume must not be negative")
        object.__setattr__(self, "volume", volume)
        if self.trade_count < 0:
            raise DomainError(f"{self.instrument_id}: trade_count must not be negative")
        if self.high < max(self.open, self.close) or self.high < self.low:
            raise DomainError(
                f"{self.instrument_id} at {self.start}: high {self.high} is below another "
                f"price in the same bar"
            )
        if self.low > min(self.open, self.close):
            raise DomainError(
                f"{self.instrument_id} at {self.start}: low {self.low} is above another "
                f"price in the same bar"
            )

    @property
    def end(self) -> datetime:
        """Exclusive right edge of the bar's interval."""
        return self.start + self.granularity.duration

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    def contains(self, instant: datetime) -> bool:
        """Whether ``instant`` falls inside this bar's interval."""
        moment = ensure_utc(instant, what="instant")
        return self.start <= moment < self.end


@final
@dataclass(frozen=True, slots=True)
class FundingRate:
    """A perpetual instrument's funding rate for one settlement period.

    The rate is a fraction of position notional, signed: positive means longs pay
    shorts.
    """

    instrument_id: InstrumentId
    ts: datetime
    rate: Decimal
    interval: timedelta
    next_settlement: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, what="funding timestamp"))
        object.__setattr__(self, "rate", to_decimal(self.rate, what="rate"))
        if self.interval <= timedelta(0):
            raise DomainError(f"{self.instrument_id}: funding interval must be positive")
        if self.next_settlement is not None:
            object.__setattr__(
                self, "next_settlement", ensure_utc(self.next_settlement, what="next_settlement")
            )

    def charge_on(self, notional: Money) -> Money:
        """The amount a long of ``notional`` pays at one settlement.

        A negative result means the position receives funding rather than paying it.
        """
        return notional * self.rate


@final
@dataclass(frozen=True, slots=True)
class SwapRates:
    """Overnight swap rates for a forex style instrument.

    Quoted in price points per unit of the base currency per night, signed from the
    holder's perspective: a negative long rate means a long position is charged.
    """

    instrument_id: InstrumentId
    ts: datetime
    long_points: Decimal
    short_points: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, what="swap timestamp"))
        object.__setattr__(self, "long_points", to_decimal(self.long_points, what="long_points"))
        object.__setattr__(self, "short_points", to_decimal(self.short_points, what="short_points"))

    def points_for(self, side: PositionSide) -> Decimal:
        """Points per unit per night for a position of ``side``."""
        if side is PositionSide.LONG:
            return self.long_points
        if side is PositionSide.SHORT:
            return self.short_points
        return Decimal(0)

    def charge(
        self, instrument: Instrument, quantity: Decimal, side: PositionSide, nights: int = 1
    ) -> Money:
        """Swap charge in the quote currency for holding ``quantity`` for ``nights``.

        A negative result is a cost, a positive one is a credit.
        """
        if nights < 0:
            raise DomainError(f"nights must not be negative, got {nights}")
        units = abs(instrument.to_base_units(quantity))
        with exact_context():
            return Money(units * self.points_for(side) * nights, instrument.quote_currency)


# ----------------------------------------------------------------------------------
# orders and execution
# ----------------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class OrderRequest:
    """An instruction to a venue, expressed in venue-neutral terms.

    The request carries a caller supplied ``client_order_id``. Every venue we target
    accepts one, and it is what makes submission idempotent across a reconnect: if the
    acknowledgement is lost, the order can be looked up rather than sent twice.

    Quantity is unsigned and expressed in the instrument's own quantity unit; direction
    comes from ``side``.
    """

    client_order_id: str
    instrument_id: InstrumentId
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    time_in_force: TimeInForce = TimeInForce.GTC
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    trigger_condition: TriggerCondition = TriggerCondition.DEFAULT
    trailing_distance: Decimal | None = None
    expires_at: datetime | None = None
    reduce_only: bool = False
    post_only: bool = False
    take_profit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    label: str | None = None
    """Free text the adapter may pass to the venue's client tag field, for reconciling
    positions against the strategy that opened them."""

    def __post_init__(self) -> None:  # noqa: PLR0912 - one branch per invariant, kept flat
        if not self.client_order_id or self.client_order_id.strip() != self.client_order_id:
            raise DomainError(
                f"client_order_id must be non-empty and unpadded, got {self.client_order_id!r}"
            )
        quantity = to_decimal(self.quantity, what="quantity")
        if quantity <= 0:
            raise DomainError(
                f"{self.client_order_id}: quantity must be positive and unsigned; direction "
                f"is carried by `side`, got {quantity}"
            )
        object.__setattr__(self, "quantity", quantity)

        for name in (
            "limit_price",
            "trigger_price",
            "trailing_distance",
            "take_profit_price",
            "stop_loss_price",
        ):
            raw = getattr(self, name)
            if raw is None:
                continue
            value = to_decimal(raw, what=name)
            if value <= 0:
                raise DomainError(f"{self.client_order_id}: {name} must be positive, got {value}")
            object.__setattr__(self, name, value)

        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", ensure_utc(self.expires_at, what="expires_at"))

        if self.order_type.requires_limit_price and self.limit_price is None:
            raise DomainError(
                f"{self.client_order_id}: a {self.order_type} order needs a limit price"
            )
        if not self.order_type.requires_limit_price and self.limit_price is not None:
            raise DomainError(
                f"{self.client_order_id}: a {self.order_type} order must not carry a limit price"
            )
        if self.order_type.requires_trigger_price and self.trigger_price is None:
            raise DomainError(
                f"{self.client_order_id}: a {self.order_type} order needs a trigger price"
            )
        if self.order_type.requires_trailing_distance and self.trailing_distance is None:
            raise DomainError(
                f"{self.client_order_id}: a {self.order_type} order needs a trailing distance"
            )
        if not self.order_type.requires_trailing_distance and self.trailing_distance is not None:
            raise DomainError(
                f"{self.client_order_id}: a {self.order_type} order must not carry a trailing "
                f"distance"
            )
        if self.time_in_force.requires_expiry and self.expires_at is None:
            raise DomainError(
                f"{self.client_order_id}: time in force {self.time_in_force} needs an expiry"
            )
        if not self.time_in_force.requires_expiry and self.expires_at is not None:
            raise DomainError(
                f"{self.client_order_id}: time in force {self.time_in_force} must not carry an "
                f"expiry"
            )
        if self.post_only and self.order_type is not OrderType.LIMIT:
            raise DomainError(
                f"{self.client_order_id}: post_only only applies to limit orders, not "
                f"{self.order_type}"
            )
        if self.post_only and self.time_in_force.is_immediate:
            raise DomainError(
                f"{self.client_order_id}: post_only contradicts {self.time_in_force}, which "
                f"can never rest on the book"
            )

    def validate_against(self, instrument: Instrument) -> None:
        """Check the request against an instrument's own limits.

        Raises:
            DomainError: The request names a different instrument.
            InvalidQuantityError: The quantity breaches a limit or the step grid.
            InvalidPriceError: A price is off the tick grid.
        """
        if instrument.id != self.instrument_id:
            raise DomainError(
                f"{self.client_order_id}: request is for {self.instrument_id} but was checked "
                f"against {instrument.id}"
            )
        instrument.validate_quantity(self.quantity)
        for name in (
            "limit_price",
            "trigger_price",
            "take_profit_price",
            "stop_loss_price",
        ):
            value = getattr(self, name)
            if value is not None:
                instrument.validate_price(value)


@final
@dataclass(frozen=True, slots=True)
class Order:
    """The venue's view of an order.

    ``venue_order_id`` is whatever the venue calls it, kept opaque: it is a string
    here even where a venue uses integers, so that no adapter has to widen the type.
    """

    venue_order_id: str
    client_order_id: str
    instrument_id: InstrumentId
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    quantity: Decimal
    filled_quantity: Decimal
    created_at: datetime
    updated_at: datetime
    time_in_force: TimeInForce = TimeInForce.GTC
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    average_fill_price: Decimal | None = None
    reject_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", ensure_utc(self.created_at, what="created_at"))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at, what="updated_at"))
        quantity = to_decimal(self.quantity, what="quantity")
        filled = to_decimal(self.filled_quantity, what="filled_quantity")
        if quantity <= 0:
            raise DomainError(f"{self.venue_order_id}: quantity must be positive")
        if filled < 0:
            raise DomainError(f"{self.venue_order_id}: filled_quantity must not be negative")
        if filled > quantity:
            raise DomainError(f"{self.venue_order_id}: filled {filled} exceeds ordered {quantity}")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "filled_quantity", filled)
        if self.average_fill_price is not None:
            object.__setattr__(
                self,
                "average_fill_price",
                to_decimal(self.average_fill_price, what="average_fill_price"),
            )
        if self.status is OrderStatus.FILLED and filled != quantity:
            raise DomainError(
                f"{self.venue_order_id}: status is filled but {filled} of {quantity} is filled"
            )
        if self.updated_at < self.created_at:
            raise DomainError(f"{self.venue_order_id}: updated_at precedes created_at")

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_working(self) -> bool:
        return self.status.is_working

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


@final
@dataclass(frozen=True, slots=True)
class Fill:
    """One execution against an order.

    Fees are :class:`~tradingsys.core.money.Money` because their currency varies: a
    crypto venue may charge in the base asset, the quote asset, or a discount token,
    and summing those as bare numbers is how fee accounting goes wrong.
    """

    fill_id: str
    venue_order_id: str
    client_order_id: str
    instrument_id: InstrumentId
    side: OrderSide
    quantity: Decimal
    price: Decimal
    ts: datetime
    fee: Money | None = None
    liquidity: LiquidityRole = LiquidityRole.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, what="fill timestamp"))
        quantity = to_decimal(self.quantity, what="quantity")
        price = to_decimal(self.price, what="price")
        if quantity <= 0:
            raise DomainError(f"{self.fill_id}: fill quantity must be positive")
        if price <= 0:
            raise DomainError(f"{self.fill_id}: fill price must be positive")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)

    def notional(self, instrument: Instrument) -> Money:
        """Value of this fill in the quote currency."""
        return instrument.notional(self.quantity, self.price)


@final
@dataclass(frozen=True, slots=True)
class Position:
    """An open position as the venue reports it.

    ``quantity`` is unsigned; ``side`` carries the direction. On a hedging venue a
    ``position_id`` distinguishes two positions in the same instrument.
    """

    instrument_id: InstrumentId
    side: PositionSide
    quantity: Decimal
    average_price: Decimal
    unrealized_pnl: Money
    realized_pnl: Money
    margin_used: Money
    updated_at: datetime
    opened_at: datetime | None = None
    financing_paid: Money | None = None
    position_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at, what="updated_at"))
        if self.opened_at is not None:
            object.__setattr__(self, "opened_at", ensure_utc(self.opened_at, what="opened_at"))
        quantity = to_decimal(self.quantity, what="quantity")
        if quantity < 0:
            raise DomainError(
                f"{self.instrument_id}: position quantity is unsigned; direction is carried "
                f"by `side`, got {quantity}"
            )
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(
            self, "average_price", to_decimal(self.average_price, what="average_price")
        )
        if self.side is PositionSide.FLAT and quantity != 0:
            raise DomainError(f"{self.instrument_id}: a flat position must have zero quantity")
        if self.side is not PositionSide.FLAT and quantity == 0:
            raise DomainError(
                f"{self.instrument_id}: a {self.side} position must have a non-zero quantity"
            )

    @property
    def is_flat(self) -> bool:
        return self.side is PositionSide.FLAT

    @property
    def signed_quantity(self) -> Decimal:
        """Quantity signed by direction: positive long, negative short."""
        if self.side is PositionSide.LONG:
            return self.quantity
        if self.side is PositionSide.SHORT:
            return -self.quantity
        return Decimal(0)

    def closing_side(self) -> OrderSide:
        """The order side that would reduce this position.

        Raises:
            DomainError: The position is already flat.
        """
        if self.side is PositionSide.LONG:
            return OrderSide.SELL
        if self.side is PositionSide.SHORT:
            return OrderSide.BUY
        raise DomainError(f"{self.instrument_id}: a flat position has nothing to close")


@final
@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Account level state at one instant.

    Every amount is in the account's own currency, which is checked here: a venue
    reporting equity in USD and margin in EUR would otherwise produce a silently wrong
    margin ratio.
    """

    account_id: str
    ts: datetime
    balance: Money
    equity: Money
    margin_used: Money
    margin_available: Money
    unrealized_pnl: Money
    realized_pnl: Money
    open_position_count: int = 0
    position_mode: PositionMode = PositionMode.NETTING

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, what="account timestamp"))
        currency = self.balance.currency
        mismatched = [
            name
            for name in (
                "equity",
                "margin_used",
                "margin_available",
                "unrealized_pnl",
                "realized_pnl",
            )
            if getattr(self, name).currency != currency
        ]
        if mismatched:
            raise DomainError(
                f"account {self.account_id}: {', '.join(mismatched)} are not denominated in "
                f"the account currency {currency.code}"
            )
        if self.open_position_count < 0:
            raise DomainError(
                f"account {self.account_id}: open_position_count must not be negative"
            )

    @property
    def currency(self) -> object:
        return self.balance.currency

    @property
    def margin_utilisation(self) -> Decimal:
        """Used margin as a fraction of equity, zero when equity is zero or negative.

        Returning zero rather than raising keeps a margin call path from crashing at
        the exact moment it matters; callers that need to distinguish check equity.
        """
        if not self.equity.is_positive:
            return Decimal(0)
        return self.margin_used.ratio_to(self.equity)


@final
@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """A single event from a venue's execution stream.

    One type with optional payloads rather than a class hierarchy: the stream is
    consumed as a sequence and persisted to the audit log, and a flat shape survives
    both without a discriminated union at every call site.
    """

    ts: datetime
    venue: str
    order: Order | None = None
    fill: Fill | None = None
    position: Position | None = None
    account: AccountSnapshot | None = None
    sequence: int | None = None
    """Venue supplied ordering token, where one exists. Used to detect a gap after a
    reconnect."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, what="event timestamp"))
        if not any((self.order, self.fill, self.position, self.account)):
            raise DomainError(
                f"{self.venue}: an execution event must carry at least one of order, fill, "
                f"position, or account"
            )


# ----------------------------------------------------------------------------------
# capabilities
# ----------------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class VenueCapabilities:
    """What a venue actually supports.

    Declaring this explicitly is what lets shared code stay venue-neutral: rather than
    branching on a venue name, callers ask whether a feature exists and get a truthful
    answer. Every adapter must state its capabilities honestly, because the alternative
    is discovering the gap through a rejected order.
    """

    venue: str
    position_mode: PositionMode
    order_types: frozenset[OrderType]
    time_in_force: frozenset[TimeInForce]
    granularities: frozenset[Granularity]
    candle_prices: frozenset[CandlePrice]
    max_candles_per_request: int
    supports_quote_stream: bool = False
    supports_trade_stream: bool = False
    supports_order_book: bool = False
    supports_historical_candles: bool = False
    supports_reduce_only: bool = False
    supports_post_only: bool = False
    supports_attached_stops: bool = False
    """Whether take profit and stop loss can be submitted with the parent order rather
    than as separate orders after the fill."""
    supports_partial_fills: bool = True
    supports_order_modification: bool = False
    tags: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.venue:
            raise DomainError("capabilities must name a venue")
        if self.max_candles_per_request < 1:
            raise DomainError(
                f"{self.venue}: max_candles_per_request must be at least 1, got "
                f"{self.max_candles_per_request}"
            )
        if self.supports_historical_candles and not self.granularities:
            raise DomainError(
                f"{self.venue}: historical candles are supported but no granularity is listed"
            )
        if self.supports_historical_candles and not self.candle_prices:
            raise DomainError(
                f"{self.venue}: historical candles are supported but no price component is listed"
            )
        if not self.order_types:
            raise DomainError(f"{self.venue}: at least one order type must be supported")

    def supports(self, request: OrderRequest) -> bool:
        """Whether this venue can express ``request`` as written."""
        try:
            self.require_supported(request)
        except UnsupportedVenueOperationError:
            return False
        return True

    def require_supported(self, request: OrderRequest) -> None:
        """Raise unless this venue can express ``request`` as written.

        Raises:
            UnsupportedVenueOperationError: The request uses a feature the venue does
                not have.
        """
        problems: list[str] = []
        if request.order_type not in self.order_types:
            problems.append(f"order type {request.order_type.value}")
        if request.time_in_force not in self.time_in_force:
            problems.append(f"time in force {request.time_in_force.value}")
        if request.reduce_only and not self.supports_reduce_only:
            problems.append("reduce_only")
        if request.post_only and not self.supports_post_only:
            problems.append("post_only")
        if (
            request.take_profit_price is not None or request.stop_loss_price is not None
        ) and not self.supports_attached_stops:
            problems.append("attached take profit or stop loss")
        if problems:
            raise UnsupportedVenueOperationError(
                self.venue,
                f"does not support {', '.join(problems)} (order {request.client_order_id})",
            )

    def require_granularity(self, granularity: Granularity, price: CandlePrice) -> None:
        """Raise unless this venue publishes candles of this width and price component.

        Raises:
            UnsupportedVenueOperationError: The combination is unavailable.
        """
        if not self.supports_historical_candles:
            raise UnsupportedVenueOperationError(self.venue, "does not publish historical candles")
        if granularity not in self.granularities:
            available = ", ".join(sorted(item.value for item in self.granularities))
            raise UnsupportedVenueOperationError(
                self.venue,
                f"does not publish {granularity.value} candles; available: {available}",
            )
        if price not in self.candle_prices:
            available = ", ".join(sorted(item.value for item in self.candle_prices))
            raise UnsupportedVenueOperationError(
                self.venue, f"does not publish {price.value} candles; available: {available}"
            )

    @classmethod
    def minimal(cls, venue: str, position_mode: PositionMode = PositionMode.NETTING) -> Self:
        """Capabilities of a venue that can do nothing but market orders.

        The floor that every venue clears, used as a starting point when declaring a
        new adapter's capabilities.
        """
        return cls(
            venue=venue,
            position_mode=position_mode,
            order_types=frozenset({OrderType.MARKET}),
            time_in_force=frozenset({TimeInForce.IOC}),
            granularities=frozenset(),
            candle_prices=frozenset(),
            max_candles_per_request=1,
        )
