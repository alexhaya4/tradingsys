"""Live top of book from cTrader spot events.

`SPEC.md` section 8 requires continuous ingestion for both venues, and this is the
forex half of it. The venue publishes price changes as `ProtoOASpotEvent` after a
`ProtoOASubscribeSpotsReq`, delivered on the same connection as everything else and
routed here by the connection's unsolicited event handler.

Three properties of the venue's wire format drive the whole design and none of them is
obvious from the message name.

**A spot event carries bid or ask or neither.** Both fields are optional in the schema,
and the venue sends only what changed. A `Quote` needs both sides, so this stream holds
the last known bid and ask per symbol and emits a quote only once both have been seen.
Treating an event as a quote would put the previous side's price on the wrong instant,
or invent a side that was never quoted.

**Prices are integers scaled by ten to the fifth, regardless of the symbol's own
digits.** A symbol with three digits and one with five both arrive on the same scale, so
the conversion is exact and identical for every symbol, and it goes through Decimal
because a price never touches floating point.

**The timestamp is optional unless it is requested.** The subscription asks for it, and
an event that arrives without one is refused rather than stamped with our own clock. A
wrong instant in the tick table is worse than a missing tick: gap detection and every
backtest read it as truth, and nothing downstream could tell it was substituted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final, final

from tradingsys.observability.logging import get_logger
from tradingsys.venues.ctrader.framing import VENUE
from tradingsys.venues.ctrader.messages.OpenApiMessages_pb2 import (
    ProtoOASpotEvent,
    ProtoOASubscribeSpotsReq,
    ProtoOASubscribeSpotsRes,
    ProtoOAUnsubscribeSpotsReq,
    ProtoOAUnsubscribeSpotsRes,
)
from tradingsys.venues.ctrader.messages.OpenApiModelMessages_pb2 import ProtoOAPayloadType
from tradingsys.venues.ctrader.tickdata import TICK_PRICE_SCALE
from tradingsys.venues.errors import VenueResponseError
from tradingsys.venues.models import Quote

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from tradingsys.core.instrument import InstrumentId
    from tradingsys.venues.ctrader.connection import CTraderConnection
    from tradingsys.venues.ctrader.messages.OpenApiCommonMessages_pb2 import ProtoMessage

__all__ = ["CTraderSpotStream", "SpotStats"]

logger = get_logger("venues.ctrader.spots")

SUBSCRIBE_SPOTS_REQ: Final = ProtoOAPayloadType.PROTO_OA_SUBSCRIBE_SPOTS_REQ
UNSUBSCRIBE_SPOTS_REQ: Final = ProtoOAPayloadType.PROTO_OA_UNSUBSCRIBE_SPOTS_REQ
SPOT_EVENT: Final = ProtoOAPayloadType.PROTO_OA_SPOT_EVENT

MILLISECONDS_PER_SECOND: Final = 1000
"""The venue states spot timestamps in Unix milliseconds."""


@final
@dataclass(slots=True)
class SpotStats:
    """Counters that distinguish a closed market from a broken subscription.

    A forex feed is legitimately silent at the weekend, so silence alone says nothing.
    What separates the two is whether events are arriving at all and how many of them
    were usable.
    """

    subscriptions: int = 0
    events_received: int = 0
    quotes_emitted: int = 0
    half_quotes: int = 0
    """Events that moved one side while the other side had never been seen.

    Expected immediately after subscribing and a defect if it persists, because it means
    a symbol has published one side only for the whole session."""
    unknown_symbols: int = 0
    dropped_full_buffer: int = 0


@final
class CTraderSpotStream:
    """Top of book quotes for a set of cTrader symbols on one connection.

    Wire this into :class:`~tradingsys.venues.ctrader.connection.CTraderConnection` as
    its ``on_event`` handler, subscribe, then iterate :meth:`quotes`.
    """

    __slots__ = (
        "_asks",
        "_bids",
        "_queue",
        "_stats",
        "_symbols",
    )

    def __init__(
        self,
        symbols: Mapping[int, InstrumentId],
        *,
        buffer_size: int = 10_000,
    ) -> None:
        """
        Args:
            symbols: cTrader numeric symbol id to canonical instrument id. Numeric
                because the venue identifies symbols by id on the wire and never by
                name, so the caller resolves names through the symbol catalogue once
                rather than this stream resolving them per event.
            buffer_size: Quotes held between the venue's reader task and the consumer.
                Bounded on purpose: an unbounded queue turns a slow consumer into
                unbounded memory growth, which fails later and less clearly than a
                counted drop.

        Raises:
            VenueResponseError: No symbols. A subscription to nothing would sit there
                looking healthy and deliver no data.
        """
        if not symbols:
            raise VenueResponseError(
                VENUE, "a spot subscription needs at least one symbol to subscribe to"
            )
        self._symbols = dict(symbols)
        self._bids: dict[int, int] = {}
        self._asks: dict[int, int] = {}
        self._queue: asyncio.Queue[Quote] = asyncio.Queue(maxsize=buffer_size)
        self._stats = SpotStats()

    @property
    def stats(self) -> SpotStats:
        return self._stats

    @property
    def symbol_ids(self) -> tuple[int, ...]:
        return tuple(self._symbols)

    async def subscribe(self, connection: CTraderConnection) -> None:
        """Subscribe to spot events for every configured symbol.

        The venue answers with a technical spot event carrying the latest price for each
        symbol, so a quote should follow shortly even when the market is closed.

        Raises:
            VenueAuthenticationError: The connection has not completed its handshake.
            VenueResponseError: The venue refused the subscription.
        """
        request = ProtoOASubscribeSpotsReq()
        request.ctidTraderAccountId = connection.ctid_trader_account_id
        request.symbolId.extend(self._symbols)
        # Without this the timestamp field is absent from every event and the only
        # instant available would be our own receive time, which is not when the venue
        # priced it.
        request.subscribeToSpotTimestamp = True

        await connection.request(
            SUBSCRIBE_SPOTS_REQ,
            request,
            ProtoOASubscribeSpotsRes(),
            ProtoOAPayloadType.PROTO_OA_SUBSCRIBE_SPOTS_RES,
        )
        self._stats.subscriptions += 1
        logger.info(
            "ctrader spot subscription established",
            venue=VENUE,
            symbols=len(self._symbols),
            subscriptions=self._stats.subscriptions,
        )

    async def unsubscribe(self, connection: CTraderConnection) -> None:
        """Stop spot events for every configured symbol.

        Raises:
            VenueResponseError: The venue refused the request.
        """
        request = ProtoOAUnsubscribeSpotsReq()
        request.ctidTraderAccountId = connection.ctid_trader_account_id
        request.symbolId.extend(self._symbols)
        await connection.request(
            UNSUBSCRIBE_SPOTS_REQ,
            request,
            ProtoOAUnsubscribeSpotsRes(),
            ProtoOAPayloadType.PROTO_OA_UNSUBSCRIBE_SPOTS_RES,
        )
        logger.info("ctrader spot subscription released", venue=VENUE, symbols=len(self._symbols))

    def forget(self) -> None:
        """Discard the last known sides for every symbol.

        Called on reconnection. The prices held here were true of a session that has
        ended, and pairing a bid from before a disconnection with an ask from after it
        would manufacture a spread that never existed. The venue resends the latest
        price for every symbol on resubscription, so nothing is lost by forgetting.
        """
        self._bids.clear()
        self._asks.clear()

    async def handle_event(self, envelope: ProtoMessage) -> None:
        """Consume one unsolicited message from the connection.

        Anything that is not a spot event is ignored, because this connection carries
        execution and account traffic too and those are not this class's business.

        Raises:
            VenueResponseError: A spot event arrived without the timestamp the
                subscription requested, or with a price this client cannot interpret.
        """
        if envelope.payloadType != SPOT_EVENT:
            return

        event = ProtoOASpotEvent()
        event.ParseFromString(envelope.payload)
        self._stats.events_received += 1

        instrument_id = self._symbols.get(event.symbolId)
        if instrument_id is None:
            # Subscribing is per symbol, so this should not happen. Counted rather than
            # ignored because if it does, the subscription and this map disagree about
            # what was asked for and that is worth seeing.
            self._stats.unknown_symbols += 1
            logger.warning(
                "spot event for a symbol this stream did not subscribe to",
                venue=VENUE,
                symbol_id=event.symbolId,
            )
            return

        if event.HasField("bid"):
            self._bids[event.symbolId] = event.bid
        if event.HasField("ask"):
            self._asks[event.symbolId] = event.ask

        bid = self._bids.get(event.symbolId)
        ask = self._asks.get(event.symbolId)
        if bid is None or ask is None:
            # One side has moved and the other has never been quoted. There is no honest
            # Quote to make from this, so it is counted and dropped.
            self._stats.half_quotes += 1
            return

        if not event.HasField("timestamp"):
            raise VenueResponseError(
                VENUE,
                f"spot event for symbol {event.symbolId} carries no timestamp, but the "
                f"subscription set subscribeToSpotTimestamp. Substituting our own clock "
                f"would record an instant the venue never quoted, which gap detection "
                f"and every backtest would read as truth",
            )

        quote = Quote(
            instrument_id=instrument_id,
            ts=self._instant(event.timestamp),
            bid=self._price(bid),
            ask=self._price(ask),
            # The venue publishes no size on a spot event and no indicative flag, so
            # neither is invented here. A missing size is None and not zero, because
            # zero is a claim about depth this feed never made.
            bid_size=None,
            ask_size=None,
        )

        try:
            self._queue.put_nowait(quote)
        except asyncio.QueueFull:
            self._stats.dropped_full_buffer += 1
            logger.warning(
                "spot quote dropped, consumer is not keeping up",
                venue=VENUE,
                symbol_id=event.symbolId,
                dropped=self._stats.dropped_full_buffer,
            )
            return
        self._stats.quotes_emitted += 1

    async def quotes(self) -> AsyncIterator[Quote]:
        """Yield quotes as they arrive, until cancelled.

        Does not reconnect. The connection owns its own lifecycle and a caller that
        needs quotes across reconnections drives both, calling :meth:`forget` and
        :meth:`subscribe` on each new connection.
        """
        while True:
            yield await self._queue.get()

    @staticmethod
    def _price(scaled: int) -> Decimal:
        """Convert a wire price to an exact Decimal.

        The scale is ten to the fifth for every symbol regardless of its own digits,
        established by measurement against the venue and recorded in `PROGRESS.md`.
        """
        return Decimal(scaled) / TICK_PRICE_SCALE

    @staticmethod
    def _instant(milliseconds: int) -> datetime:
        """Convert the venue's Unix millisecond timestamp to an aware UTC datetime.

        Integer divmod rather than a float division, because a millisecond epoch needs
        more mantissa than a double has to spare and the low digits would be discarded
        silently. The same defect was found and fixed in the Bybit REST client.
        """
        seconds, remainder = divmod(milliseconds, MILLISECONDS_PER_SECOND)
        return datetime.fromtimestamp(seconds, tz=UTC).replace(
            microsecond=remainder * MILLISECONDS_PER_SECOND
        )
