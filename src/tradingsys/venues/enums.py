"""Enumerations shared by the venue interfaces.

These are the system's own vocabulary. Adapters translate their venue's spelling into
these values and back, which is what keeps a forex broker's ``MARKET_IF_TOUCHED`` and
an exchange's ``take_profit_market`` from leaking into strategy code.

Members whose meaning is not obvious carry their definition, because a mistaken reading
of a time in force or a candle price component is the kind of error that only shows up
in live trading.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Self, final

from tradingsys.core.errors import DomainError
from tradingsys.core.numeric import to_decimal

__all__ = [
    "CandlePrice",
    "Granularity",
    "LiquidityRole",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PositionMode",
    "PositionSide",
    "TimeInForce",
    "TriggerCondition",
]


class PositionMode(StrEnum):
    """Whether a venue keeps one position per instrument or several.

    Netting venues collapse buys and sells into a single signed position, so a sell
    against a long reduces it. Hedging venues let opposing positions coexist, which
    changes what "close the position" means and whether a reduce-only flag is
    meaningful. Strategy code must not assume either.
    """

    NETTING = "netting"
    HEDGING = "hedging"


class OrderSide(StrEnum):
    """Direction of an order."""

    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> OrderSide:
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY

    @property
    def sign(self) -> int:
        """+1 for a buy, -1 for a sell, for turning a size into a signed exposure."""
        return 1 if self is OrderSide.BUY else -1


class PositionSide(StrEnum):
    """Direction of an open position.

    ``FLAT`` exists so that a position record can describe a closed position without
    the caller having to treat ``None`` as a third state.
    """

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"

    @classmethod
    def from_signed_quantity(cls, quantity: object) -> PositionSide:
        """Derive the side from a signed size, where positive is long."""
        value = to_decimal(quantity, what="quantity")
        if value > 0:
            return cls.LONG
        if value < 0:
            return cls.SHORT
        return cls.FLAT


class OrderType(StrEnum):
    """How an order is priced and triggered."""

    MARKET = "market"
    """Fill immediately at whatever the venue offers."""

    LIMIT = "limit"
    """Fill at the limit price or better, resting until it can."""

    STOP = "stop"
    """Become a market order once the trigger price trades."""

    STOP_LIMIT = "stop_limit"
    """Become a limit order once the trigger price trades."""

    TRAILING_STOP = "trailing_stop"
    """A stop whose trigger follows the market by a fixed distance."""

    @property
    def requires_limit_price(self) -> bool:
        return self in (OrderType.LIMIT, OrderType.STOP_LIMIT)

    @property
    def requires_trigger_price(self) -> bool:
        return self in (OrderType.STOP, OrderType.STOP_LIMIT)

    @property
    def requires_trailing_distance(self) -> bool:
        return self is OrderType.TRAILING_STOP


class TimeInForce(StrEnum):
    """How long an order remains active."""

    GTC = "gtc"
    """Good until cancelled."""

    GTD = "gtd"
    """Good until a specified datetime, which the request must carry."""

    IOC = "ioc"
    """Immediate or cancel: fill what is available now, cancel the rest."""

    FOK = "fok"
    """Fill or kill: fill the entire quantity now, or nothing."""

    DAY = "day"
    """Expires at the end of the venue's trading day."""

    @property
    def requires_expiry(self) -> bool:
        return self is TimeInForce.GTD

    @property
    def is_immediate(self) -> bool:
        """Whether the order can never rest on the book."""
        return self in (TimeInForce.IOC, TimeInForce.FOK)


class TriggerCondition(StrEnum):
    """Which price a stop or trailing order watches.

    Forex brokers commonly trigger on the bid for a sell and the ask for a buy, while
    crypto venues usually trigger on the last trade or an index price. The difference
    moves the effective stop by the spread, so it is explicit rather than assumed.
    """

    DEFAULT = "default"
    """Whatever the venue does by default, recorded as such rather than guessed at."""

    LAST = "last"
    BID = "bid"
    ASK = "ask"
    MID = "mid"
    MARK = "mark"
    INDEX = "index"


class OrderStatus(StrEnum):
    """Lifecycle state of an order."""

    PENDING_SUBMIT = "pending_submit"
    """Created locally, not yet acknowledged by the venue."""

    OPEN = "open"
    """Live at the venue, unfilled or partially filled."""

    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        """Whether no further events can arrive for this order."""
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )

    @property
    def is_working(self) -> bool:
        """Whether the order can still consume margin or take a fill."""
        return self in (
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        )


class LiquidityRole(StrEnum):
    """Whether a fill added or removed liquidity, which sets the fee tier."""

    MAKER = "maker"
    TAKER = "taker"
    UNKNOWN = "unknown"


class CandlePrice(StrEnum):
    """Which price series a candle is built from.

    Forex brokers publish separate bid, ask, and mid candles, since there is no
    consolidated tape. Crypto exchanges publish traded prices. Asking for a component
    the venue does not offer is an error rather than a silent substitution, because
    backtesting against mid and trading against ask is a systematic bias.
    """

    BID = "bid"
    ASK = "ask"
    MID = "mid"
    TRADE = "trade"


@final
class Granularity(StrEnum):
    """Candle bar width.

    The value is the canonical name used in storage and logs. :attr:`duration` gives
    the exact width; adapters map to their venue's own spelling.
    """

    S1 = "1s"
    S5 = "5s"
    S15 = "15s"
    S30 = "30s"
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    H12 = "12h"
    D1 = "1d"
    W1 = "1w"

    @property
    def duration(self) -> timedelta:
        """Exact width of one bar.

        Weekly bars are seven days: this system does not model calendar months, whose
        variable length would make bar arithmetic ambiguous.
        """
        return _GRANULARITY_DURATIONS[self]

    @property
    def seconds(self) -> int:
        return int(self.duration.total_seconds())

    @classmethod
    def from_duration(cls, duration: timedelta) -> Self:
        """The granularity of exactly this width.

        Raises:
            DomainError: No granularity has that width.
        """
        for granularity, width in _GRANULARITY_DURATIONS.items():
            if width == duration:
                return cls(granularity)
        supported = ", ".join(member.value for member in cls)
        raise DomainError(f"no granularity of width {duration}; supported: {supported}")


_GRANULARITY_DURATIONS: dict[Granularity, timedelta] = {
    Granularity.S1: timedelta(seconds=1),
    Granularity.S5: timedelta(seconds=5),
    Granularity.S15: timedelta(seconds=15),
    Granularity.S30: timedelta(seconds=30),
    Granularity.M1: timedelta(minutes=1),
    Granularity.M5: timedelta(minutes=5),
    Granularity.M15: timedelta(minutes=15),
    Granularity.M30: timedelta(minutes=30),
    Granularity.H1: timedelta(hours=1),
    Granularity.H4: timedelta(hours=4),
    Granularity.H12: timedelta(hours=12),
    Granularity.D1: timedelta(days=1),
    Granularity.W1: timedelta(days=7),
}
