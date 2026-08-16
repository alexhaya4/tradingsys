"""cTrader symbol metadata, mapped into the venue neutral instrument model.

Four things about this venue's metadata need care, and each of them is a way to be
quietly wrong rather than loudly broken.

**Volumes are in hundredths.** ``minVolume``, ``stepVolume``, ``maxVolume``, and
``lotSize`` are all expressed in cents of the base asset, so a ``minVolume`` of 100000
means 1000 units and not 100000 of anything. Reading them as units overstates every
size by a hundred, which at this account's capital is the difference between a
tradeable instrument and one that is excluded.

**``digits`` and ``pipPosition`` are different numbers.** ``digits`` is how many
decimals the price carries, so the smallest price movement is ten to the minus
``digits``. ``pipPosition`` is where the pip sits within those digits. On a five digit
EUR/USD they are 5 and 4: the tick is 0.00001 and the pip is 0.0001. Using one for the
other is off by a factor of ten in the sizing arithmetic.

**Swap arrives as a double.** The broker quotes a swap in pips or percent and protobuf
carries it as a binary double, so the value that was meant is the shortest decimal
that maps back to the same double, not its exact binary expansion. See
:func:`~tradingsys.core.numeric.decimal_from_double`, which exists for this and says
why it is not the same conversion the Dukascopy reader uses.

**The trading week is published as seconds from Sunday midnight** in the symbol's own
timezone, which is not necessarily UTC and is not necessarily the broker's. It is
mapped into real weekday and wall clock session boundaries rather than approximated,
because gap detection compares recorded coverage against it and a schedule that is
half a day out reports gaps every weekend.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from tradingsys.core.financing import FinancingModel, FinancingSpec
from tradingsys.core.instrument import (
    AssetClass,
    Instrument,
    InstrumentId,
    InstrumentStatus,
    QuantityUnit,
)
from tradingsys.core.numeric import decimal_from_double
from tradingsys.core.schedule import TradingSchedule, Weekday, WeeklySession
from tradingsys.venues.ctrader.framing import VENUE
from tradingsys.venues.ctrader.messages.OpenApiModelMessages_pb2 import (
    ProtoOASwapCalculationType,
    ProtoOATradingMode,
)
from tradingsys.venues.errors import VenueResponseError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tradingsys.core.currency import CurrencyRegistry
    from tradingsys.venues.ctrader.messages.OpenApiModelMessages_pb2 import (
        ProtoOALightSymbol,
        ProtoOASymbol,
    )

__all__ = [
    "CENTS_PER_UNIT",
    "SwapConvention",
    "instrument_from_symbol",
    "swap_convention",
]

CENTS_PER_UNIT: Final = Decimal(100)
"""Volume fields are published in hundredths of a base unit.

Named rather than written as a bare 100 at each use, because every one of those uses
is a sizing calculation and a missing division is a hundredfold error in a position
size that no test of the arithmetic alone would notice.
"""

SECONDS_PER_DAY: Final = 86_400

_WEEKDAY_FROM_VENUE: Final = {
    1: Weekday.MONDAY,
    2: Weekday.TUESDAY,
    3: Weekday.WEDNESDAY,
    4: Weekday.THURSDAY,
    5: Weekday.FRIDAY,
    6: Weekday.SATURDAY,
    7: Weekday.SUNDAY,
}
"""The venue numbers Monday as 1 and reserves 0 for NONE; this package matches
``date.weekday`` and numbers Monday as 0. The offset is not a detail to inline."""


class SwapConvention:
    """How swap is charged on one symbol, kept in the venue's own terms.

    Attributes:
        long_rate: Charge applied to a long position, in the unit named by
            :attr:`calculated_in`.
        short_rate: Charge applied to a short position.
        calculated_in: ``pips`` or ``percentage``, the venue's own two options. A
            percentage is annual.
        period_hours: How often swap is charged. 24 means once a day.
        first_charge_minutes_after_midnight_utc: When the first charge of the day
            lands.
        triple_day: Weekday on which three nights are charged at once, covering the
            weekend value dates.
        charged_at_weekends: Whether the venue charges on Saturday and Sunday too, in
            which case the triple day convention does not apply the same way.
    """

    __slots__ = (
        "calculated_in",
        "charged_at_weekends",
        "first_charge_minutes_after_midnight_utc",
        "long_rate",
        "period_hours",
        "short_rate",
        "triple_day",
    )

    def __init__(
        self,
        *,
        long_rate: Decimal,
        short_rate: Decimal,
        calculated_in: str,
        period_hours: int,
        first_charge_minutes_after_midnight_utc: int,
        triple_day: Weekday | None,
        charged_at_weekends: bool,
    ) -> None:
        self.long_rate = long_rate
        self.short_rate = short_rate
        self.calculated_in = calculated_in
        self.period_hours = period_hours
        self.first_charge_minutes_after_midnight_utc = first_charge_minutes_after_midnight_utc
        self.triple_day = triple_day
        self.charged_at_weekends = charged_at_weekends

    def __repr__(self) -> str:
        return (
            f"SwapConvention(long={self.long_rate}, short={self.short_rate}, "
            f"in={self.calculated_in}, every={self.period_hours}h, "
            f"triple_day={self.triple_day}, weekends={self.charged_at_weekends})"
        )


def swap_convention(symbol: ProtoOASymbol) -> SwapConvention:
    """Read the swap charging convention off a symbol.

    Raises:
        VenueResponseError: The venue reported a swap calculation type this client
            does not know, which must not be guessed at: reading a percentage as pips
            misstates the carry by orders of magnitude.
    """
    # A proto2 closed enum drops a value it does not know into unknown fields, so an
    # unrecognised swap type arrives as an absent field reading as the PIPS default
    # rather than as a value that can be rejected. The venue sets this field on every
    # symbol, observed across the live catalogue, so absence means exactly that case
    # and is refused.
    if not symbol.HasField("swapCalculationType"):
        raise VenueResponseError(
            VENUE,
            f"symbol {symbol.symbolId} published no swap calculation type. The venue "
            f"sets it on every symbol, so an absent field means it sent a value this "
            f"client does not know, which protobuf has silently replaced with the PIPS "
            f"default. Reading an annual percentage as pips would misstate the carry by "
            f"orders of magnitude.",
        )
    calculation = symbol.swapCalculationType
    if calculation == ProtoOASwapCalculationType.PIPS:
        calculated_in = "pips"
    elif calculation == ProtoOASwapCalculationType.PERCENTAGE:
        calculated_in = "percentage"
    else:
        raise VenueResponseError(
            VENUE,
            f"symbol {symbol.symbolId} reported swap calculation type {calculation}, "
            f"which this client does not know. Guessing between pips and an annual "
            f"percentage would misstate the carry by orders of magnitude.",
        )

    triple = symbol.swapRollover3Days if symbol.HasField("swapRollover3Days") else 0
    return SwapConvention(
        long_rate=decimal_from_double(symbol.swapLong, what="swapLong"),
        short_rate=decimal_from_double(symbol.swapShort, what="swapShort"),
        calculated_in=calculated_in,
        period_hours=symbol.swapPeriod if symbol.HasField("swapPeriod") else 24,
        first_charge_minutes_after_midnight_utc=(
            symbol.swapTime if symbol.HasField("swapTime") else 0
        ),
        triple_day=_WEEKDAY_FROM_VENUE.get(triple),
        charged_at_weekends=bool(symbol.chargeSwapAtWeekends),
    )


def instrument_from_symbol(
    symbol: ProtoOASymbol,
    light: ProtoOALightSymbol,
    currencies: CurrencyRegistry,
    *,
    base_asset: str,
    quote_asset: str,
) -> Instrument:
    """Build an instrument from a symbol's full and lightweight records.

    Both records are needed. The full :class:`ProtoOASymbol` carries the precision and
    volume fields; the lightweight one carries the venue's own name for the symbol and
    its asset ids, and the venue does not repeat the name in the full record.

    Args:
        symbol: The full symbol record, from a symbol by id request.
        light: The matching lightweight record, from the symbol list.
        currencies: Registry used to resolve the asset names. An asset it does not
            know raises rather than being invented with a guessed precision.
        base_asset: Name of the base asset, resolved from the venue's asset list.
        quote_asset: Name of the quote asset.

    Raises:
        VenueResponseError: A field this system needs is absent, or a value is one
            this client cannot interpret.
    """
    base = currencies.get(base_asset)
    quote = currencies.get(quote_asset)

    digits = symbol.digits
    if digits < 0:
        raise VenueResponseError(VENUE, f"symbol {symbol.symbolId} reported {digits} price digits")
    price_increment = Decimal(1).scaleb(-digits)
    pip_size = Decimal(1).scaleb(-symbol.pipPosition)

    min_volume = _volume(symbol, "minVolume", symbol.minVolume)
    step_volume = _volume(symbol, "stepVolume", symbol.stepVolume)
    max_volume = (
        _volume(symbol, "maxVolume", symbol.maxVolume) if symbol.HasField("maxVolume") else None
    )

    return Instrument(
        id=InstrumentId(venue=VENUE, symbol=f"{base.code}/{quote.code}"),
        venue_symbol=light.symbolName,
        asset_class=AssetClass.FX_SPOT,
        base_currency=base,
        quote_currency=quote,
        # A margin position on a forex venue never settles in the base asset. It is
        # marked, and its profit and loss realised, in the quote.
        settlement_currency=quote,
        price_increment=price_increment,
        price_precision=digits,
        pip_size=pip_size,
        # Orders are sized in base units. The venue transports them as hundredths, and
        # that conversion is done here so that nothing above this layer has to know.
        quantity_unit=QuantityUnit.UNITS,
        contract_size=Decimal(1),
        quantity_increment=step_volume,
        min_quantity=min_volume,
        max_quantity=max_volume,
        # No cash minimum is published for these symbols. The binding floor is the
        # volume minimum above, and asserting a notional the venue never stated would
        # exclude instruments for a limit that does not exist.
        min_notional=None,
        financing=_financing(symbol),
        schedule=_schedule(symbol),
        status=_status(symbol),
    )


def _volume(symbol: ProtoOASymbol, field: str, value: int) -> Decimal:
    """Convert a volume in hundredths of a base unit into base units."""
    if value <= 0:
        raise VenueResponseError(
            VENUE,
            f"symbol {symbol.symbolId} reported {field}={value}. A non positive volume "
            f"cannot be sized against and must not be treated as unlimited.",
        )
    return Decimal(value) / CENTS_PER_UNIT


def _financing(symbol: ProtoOASymbol) -> FinancingSpec:
    """Map the venue's swap convention onto the venue neutral financing spec."""
    convention = swap_convention(symbol)
    minutes = convention.first_charge_minutes_after_midnight_utc
    if not 0 <= minutes < 24 * 60:
        raise VenueResponseError(
            VENUE,
            f"symbol {symbol.symbolId} reported a swap time of {minutes} minutes after "
            f"midnight, which is not a time of day",
        )
    return FinancingSpec(
        model=FinancingModel.SWAP_POINTS,
        rollover_time=time(hour=minutes // 60, minute=minutes % 60),
        triple_rollover_day=convention.triple_day,
    )


def _schedule(symbol: ProtoOASymbol) -> TradingSchedule:
    """Map the venue's weekly intervals into sessions.

    The venue publishes each interval as a start and end in seconds from Sunday
    midnight, in ``scheduleTimeZone``. A schedule with no intervals raises rather than
    defaulting to always open, because a symbol that is always open and a symbol whose
    hours were not reported are different facts and only one of them is safe.
    """
    if not symbol.schedule:
        raise VenueResponseError(
            VENUE,
            f"symbol {symbol.symbolId} published no trading intervals. An unknown "
            f"schedule must not be read as continuously open.",
        )
    timezone = symbol.scheduleTimeZone if symbol.HasField("scheduleTimeZone") else "UTC"
    sessions = tuple(
        WeeklySession(
            open_day=_day_of(interval.startSecond),
            open_time=_time_of(interval.startSecond),
            close_day=_day_of(interval.endSecond),
            close_time=_time_of(interval.endSecond),
        )
        for interval in symbol.schedule
    )
    return TradingSchedule(timezone=timezone, sessions=sessions)


def _day_of(second_of_week: int) -> Weekday:
    """The weekday a second offset from Sunday midnight falls on.

    The venue counts from Sunday and this package counts from Monday, so the two
    numberings differ by one day and the conversion is not the identity.
    """
    days_from_sunday = (second_of_week // SECONDS_PER_DAY) % 7
    # Sunday is 6 here and 0 there, so shifting back by one lands Monday on 0.
    return Weekday((days_from_sunday + 6) % 7)


def _time_of(second_of_week: int) -> time:
    """The wall clock time a second offset from Sunday midnight falls at."""
    second_of_day = second_of_week % SECONDS_PER_DAY
    return time(
        hour=second_of_day // 3600,
        minute=(second_of_day % 3600) // 60,
        second=second_of_day % 60,
    )


def _status(symbol: ProtoOASymbol) -> InstrumentStatus:
    """Map the venue's trading mode onto the instrument status.

    An unknown mode is refused rather than assumed active. Treating a mode this client
    does not recognise as tradeable is how an order reaches a symbol the venue has
    closed.
    """
    # As with the swap type, an unknown trading mode arrives as an absent field that
    # reads as the ENABLED default. The venue sets it on every symbol, so absence is
    # the signal that it sent something this client cannot interpret, and defaulting to
    # tradeable is the one answer that must not be given.
    if not symbol.HasField("tradingMode"):
        raise VenueResponseError(
            VENUE,
            f"symbol {symbol.symbolId} published no trading mode. The venue sets it on "
            f"every symbol, so an absent field means it sent a mode this client does not "
            f"know, which protobuf has silently replaced with the ENABLED default. "
            f"Treating that as tradeable would send orders to a symbol the venue may "
            f"have closed.",
        )
    mode = symbol.tradingMode
    if mode == ProtoOATradingMode.ENABLED:
        return InstrumentStatus.ACTIVE
    if mode in (
        ProtoOATradingMode.DISABLED_WITHOUT_PENDINGS_EXECUTION,
        ProtoOATradingMode.DISABLED_WITH_PENDINGS_EXECUTION,
    ):
        return InstrumentStatus.HALTED
    if mode == ProtoOATradingMode.CLOSE_ONLY_MODE:
        return InstrumentStatus.REDUCE_ONLY
    raise VenueResponseError(
        VENUE,
        f"symbol {symbol.symbolId} reported trading mode {mode}, which this client does "
        f"not know. Treating an unrecognised mode as tradeable would send orders to a "
        f"symbol the venue may have closed.",
    )


def symbol_names(symbols: Sequence[ProtoOALightSymbol]) -> dict[str, int]:
    """Index the venue's symbol ids by their names, for resolving a configured symbol."""
    return {symbol.symbolName: symbol.symbolId for symbol in symbols}
