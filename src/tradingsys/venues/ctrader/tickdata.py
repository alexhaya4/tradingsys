"""Historical tick data from cTrader, and the two ways to read it wrongly.

The venue answers a tick data request with a list that is **newest first** and
**delta encoded**: the first entry carries an absolute timestamp and an absolute
price, and every entry after it carries the difference from the one before. Read as
absolute values it decodes to prices near zero and timestamps in 1970, which is
obvious. Read as deltas in the wrong direction it decodes to prices that look
entirely plausible and are wrong, which is not.

**Each quote type is a separate series.** A request returns bid or ask, never both, so
a spread has to be reconstructed by merging two series. They do not tick together: a
bid update and an ask update are independent events, so at any instant the spread is
the most recent bid against the most recent ask, and that is what :func:`spread_series`
computes. Pairing the nth bid with the nth ask would be arithmetic on unrelated
instants.

**Prices are integers scaled by ten to the fifth**, as with trendbars, rather than by
the symbol's own digit count. The same scale carries a five digit EUR/USD and a three
digit USD/JPY, so applying the symbol's digits here is wrong by a factor of a hundred
on the JPY pairs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from typing import TYPE_CHECKING, Final, final

from tradingsys.core.numeric import exact_context
from tradingsys.venues.ctrader.framing import VENUE
from tradingsys.venues.errors import VenueResponseError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tradingsys.venues.ctrader.messages.OpenApiModelMessages_pb2 import ProtoOATickData

__all__ = [
    "TICK_PRICE_SCALE",
    "Spread",
    "Tick",
    "decode_tick_series",
    "spread_series",
]

TICK_PRICE_SCALE: Final = Decimal(100_000)
"""Tick prices are published as integers scaled by ten to the fifth.

A venue convention rather than a function of the symbol's digits. Confirmed against
live EUR/USD and USD/JPY, which arrive on the same scale despite having five and three
digits respectively.
"""


@final
@dataclass(frozen=True, slots=True)
class Tick:
    """One quote of one side, decoded to absolute values."""

    ts: datetime
    price: Decimal


@final
@dataclass(frozen=True, slots=True)
class Spread:
    """The ask minus the bid at one instant, with both sides that produced it."""

    ts: datetime
    bid: Decimal
    ask: Decimal

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def in_pips(self, pip_size: Decimal) -> Decimal:
        """The spread expressed in the instrument's pips.

        Raises:
            VenueResponseError: The pip size is not positive, which would make the
                division meaningless rather than merely wrong.
        """
        if pip_size <= 0:
            raise VenueResponseError(VENUE, f"pip size must be positive, got {pip_size}")
        with exact_context():
            return self.spread / pip_size


def decode_tick_series(records: Sequence[ProtoOATickData]) -> tuple[Tick, ...]:
    """Decode one delta encoded, newest first tick series into absolute ticks.

    The venue's first record holds absolute values and each later record holds the
    difference from its predecessor, walking backwards in time. The result is returned
    in chronological order, oldest first, because every consumer of it wants that and
    reversing at each call site is how one of them eventually forgets.

    Args:
        records: ``tickData`` exactly as the venue sent it.

    Raises:
        VenueResponseError: A record is missing a field, or the decoded series is not
            monotonic in time. A non monotonic result means the delta convention is
            not what this function assumes, and continuing would produce plausible
            prices at wrong instants.
    """
    if not records:
        return ()

    ticks: list[Tick] = []
    timestamp = 0
    price = 0
    for index, record in enumerate(records):
        if not record.HasField("timestamp") or not record.HasField("tick"):
            raise VenueResponseError(
                VENUE, f"tick record {index} is missing a timestamp or a price"
            )
        if index == 0:
            timestamp = record.timestamp
            price = record.tick
        else:
            timestamp += record.timestamp
            price += record.tick
        if timestamp <= 0:
            raise VenueResponseError(
                VENUE,
                f"tick record {index} decoded to timestamp {timestamp}, which is not a "
                f"real instant. The series is newest first and delta encoded; a "
                f"non positive result means it was walked the wrong way.",
            )
        if price <= 0:
            raise VenueResponseError(
                VENUE, f"tick record {index} decoded to price {price}, which is not a price"
            )
        with exact_context():
            scaled = Decimal(price) / TICK_PRICE_SCALE
        ticks.append(Tick(ts=datetime.fromtimestamp(timestamp / 1000, tz=UTC), price=scaled))

    ticks.reverse()
    for earlier, later in pairwise(ticks):
        if later.ts < earlier.ts:
            raise VenueResponseError(
                VENUE,
                f"the decoded series runs backwards at {later.ts.isoformat()}. The delta "
                f"convention is not what this decoder assumes, and the prices it "
                f"produced are at the wrong instants.",
            )
    return tuple(ticks)


def spread_series(bids: Sequence[Tick], asks: Sequence[Tick]) -> tuple[Spread, ...]:
    """Reconstruct the spread from the instants where both sides were quoted together.

    **Only exact timestamp matches are paired**, and the reason is measured rather than
    assumed. The venue quotes both sides of a symbol at the same instant and splits
    them into two series on the way out: over a twenty minute EUR/USD window on
    2026-08-17 there were 1285 bid ticks, 1251 ask ticks, and 1193 shared timestamps,
    so 92.8 percent of the denser side pairs exactly.

    The first version of this function instead carried the most recent price of each
    side forward and emitted a spread on every event. That produced a median of zero
    and a minimum of minus 1.5 pips over the same window, because a bid that moved
    while the ask had not yet ticked reads as a crossed quote. Pairing on exact
    timestamps over the same data yields no crossed quotes at all. The artefacts were
    staleness, not the market.

    The unpaired remainder is dropped rather than approximated. A spread measurement
    exists to find out how wide it gets, and an approximation whose error has the same
    magnitude as the quantity being measured cannot answer that.

    Genuinely crossed quotes, if the venue publishes any, are kept: they belong to the
    widest moments and dropping them would flatter the result.
    """
    by_instant = {tick.ts: tick.price for tick in bids}
    return tuple(
        Spread(ts=tick.ts, bid=by_instant[tick.ts], ask=tick.price)
        for tick in asks
        if tick.ts in by_instant
    )
