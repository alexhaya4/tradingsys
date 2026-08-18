"""Spot subscription behaviour, and the three wire facts the design turns on.

Each of those facts is a way the venue can hand us something that looks usable and is
not: a one sided event that would pair a price with a side never quoted, a timestamp
that is absent unless asked for, and an integer scale that is the same for every symbol
regardless of its digits. The tests that matter here are the refusals and the pairing,
not the happy path.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from tradingsys.core.errors import DomainError
from tradingsys.core.instrument import InstrumentId
from tradingsys.venues.ctrader.messages.OpenApiCommonMessages_pb2 import ProtoMessage
from tradingsys.venues.ctrader.messages.OpenApiMessages_pb2 import ProtoOASpotEvent
from tradingsys.venues.ctrader.messages.OpenApiModelMessages_pb2 import ProtoOAPayloadType
from tradingsys.venues.ctrader.spots import SPOT_EVENT, CTraderSpotStream
from tradingsys.venues.errors import VenueResponseError
from tradingsys.venues.models import Quote

pytestmark = pytest.mark.asyncio

EURUSD_SYMBOL_ID = 1
GBPUSD_SYMBOL_ID = 2
EURUSD = InstrumentId("ctrader", "EUR/USD")
GBPUSD = InstrumentId("ctrader", "GBP/USD")

SYMBOLS = {EURUSD_SYMBOL_ID: EURUSD, GBPUSD_SYMBOL_ID: GBPUSD}

# 2026-08-18T09:15:30.250Z
TIMESTAMP_MS = 1_786_965_330_250


def spot_event(
    symbol_id: int,
    *,
    bid: int | None = None,
    ask: int | None = None,
    timestamp: int | None = TIMESTAMP_MS,
) -> ProtoMessage:
    """One spot event on the wire, with only the fields the venue would have sent."""
    event = ProtoOASpotEvent()
    event.ctidTraderAccountId = 48_268_952
    event.symbolId = symbol_id
    if bid is not None:
        event.bid = bid
    if ask is not None:
        event.ask = ask
    if timestamp is not None:
        event.timestamp = timestamp
    envelope = ProtoMessage()
    envelope.payloadType = SPOT_EVENT
    envelope.payload = event.SerializeToString()
    return envelope


def stream(*, buffer_size: int = 10_000) -> CTraderSpotStream:
    return CTraderSpotStream(SYMBOLS, buffer_size=buffer_size)


async def drain(spots: CTraderSpotStream, count: int) -> list[Quote]:
    """Take exactly ``count`` quotes, failing rather than hanging if they never come."""
    quotes: list[Quote] = []
    iterator = spots.quotes()
    for _ in range(count):
        quotes.append(await asyncio.wait_for(anext(iterator), timeout=1))
    return quotes


class TestSidePairing:
    async def test_one_side_alone_emits_nothing(self) -> None:
        """A bid with no ask ever seen is not a quote and must not become one."""
        spots = stream()
        await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, bid=115_692))

        assert spots.stats.events_received == 1
        assert spots.stats.quotes_emitted == 0
        assert spots.stats.half_quotes == 1

    async def test_second_side_completes_the_quote(self) -> None:
        spots = stream()
        await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, bid=115_692))
        await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, ask=115_700))

        (quote,) = await drain(spots, 1)
        assert quote.instrument_id == EURUSD
        assert quote.bid == Decimal("1.15692")
        assert quote.ask == Decimal("1.15700")
        assert spots.stats.quotes_emitted == 1

    async def test_a_moving_side_carries_the_other_forward(self) -> None:
        """The venue sends only what changed, so the unchanged side must persist."""
        spots = stream()
        await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, bid=115_692, ask=115_700))
        await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, bid=115_695))

        first, second = await drain(spots, 2)
        assert second.bid == Decimal("1.15695")
        assert second.ask == first.ask

    async def test_symbols_do_not_share_sides(self) -> None:
        """A bid on one symbol must never complete a quote on another."""
        spots = stream()
        await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, bid=115_692))
        await spots.handle_event(spot_event(GBPUSD_SYMBOL_ID, ask=127_400))

        assert spots.stats.quotes_emitted == 0
        assert spots.stats.half_quotes == 2

    async def test_forget_discards_sides_across_a_reconnection(self) -> None:
        """Pairing a pre-disconnection bid with a post-disconnection ask invents a
        spread that never existed, so a reconnection starts from nothing."""
        spots = stream()
        await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, bid=115_692))
        spots.forget()
        await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, ask=115_700))

        assert spots.stats.quotes_emitted == 0


class TestTimestamp:
    async def test_an_event_without_a_timestamp_is_refused(self) -> None:
        """Stamping it with our own clock would record an instant the venue never
        quoted, and gap detection would read it as truth."""
        spots = stream()
        await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, bid=115_692))

        with pytest.raises(VenueResponseError, match="carries no timestamp"):
            await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, ask=115_700, timestamp=None))

    async def test_the_venue_instant_is_used_exactly(self) -> None:
        spots = stream()
        await spots.handle_event(
            spot_event(EURUSD_SYMBOL_ID, bid=115_692, ask=115_700, timestamp=TIMESTAMP_MS)
        )

        (quote,) = await drain(spots, 1)
        assert quote.ts.timestamp() == TIMESTAMP_MS / 1000
        assert quote.ts.tzinfo is not None

    async def test_milliseconds_survive_the_conversion(self) -> None:
        """A millisecond epoch needs more mantissa than a double has, so the low digits
        must not be lost the way they were in the Bybit REST client."""
        spots = stream()
        await spots.handle_event(
            spot_event(EURUSD_SYMBOL_ID, bid=1, ask=2, timestamp=1_786_965_330_999)
        )

        (quote,) = await drain(spots, 1)
        assert quote.ts.microsecond == 999_000


class TestPriceScale:
    @pytest.mark.parametrize(
        ("scaled", "expected"),
        [
            (115_692, "1.15692"),
            (127_400, "1.27400"),
            # USD/JPY quotes to three digits and still arrives on the same scale.
            (14_752_300, "147.52300"),
            (1, "0.00001"),
        ],
    )
    async def test_the_scale_is_ten_to_the_fifth_for_every_symbol(
        self, scaled: int, expected: str
    ) -> None:
        spots = stream()
        await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, bid=scaled, ask=scaled))

        (quote,) = await drain(spots, 1)
        # Equality rather than string form: dividing by the scale normalises trailing
        # zeros, so 1.15800 becomes 1.158. That matches the tick reader already verified
        # byte for byte against a recorded hour, and NUMERIC compares numerically.
        assert quote.bid == Decimal(expected)

    async def test_prices_are_decimal_and_exact(self) -> None:
        spots = stream()
        await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, bid=115_692, ask=115_693))

        (quote,) = await drain(spots, 1)
        assert isinstance(quote.bid, Decimal)
        assert quote.ask - quote.bid == Decimal("0.00001")


class TestRefusalsAndCounters:
    async def test_a_subscription_to_nothing_is_refused(self) -> None:
        with pytest.raises(VenueResponseError, match="at least one symbol"):
            CTraderSpotStream({})

    async def test_a_non_spot_message_is_ignored(self) -> None:
        """This connection also carries execution and account traffic."""
        spots = stream()
        envelope = ProtoMessage()
        envelope.payloadType = ProtoOAPayloadType.PROTO_OA_ACCOUNT_AUTH_RES
        await spots.handle_event(envelope)

        assert spots.stats.events_received == 0

    async def test_an_unsubscribed_symbol_is_counted_not_emitted(self) -> None:
        spots = stream()
        await spots.handle_event(spot_event(999, bid=100_000, ask=100_001))

        assert spots.stats.unknown_symbols == 1
        assert spots.stats.quotes_emitted == 0

    async def test_a_full_buffer_drops_and_counts_rather_than_growing(self) -> None:
        """An unbounded queue turns a slow consumer into unbounded memory growth, which
        fails later and less clearly than a counted drop."""
        spots = stream(buffer_size=2)
        for tick in range(5):
            await spots.handle_event(
                spot_event(EURUSD_SYMBOL_ID, bid=115_692 + tick, ask=115_700 + tick)
            )

        assert spots.stats.quotes_emitted == 2
        assert spots.stats.dropped_full_buffer == 3

    async def test_a_bid_above_the_ask_is_refused_by_the_domain(self) -> None:
        """The Quote model rejects a crossed book, and this stream does not bypass it.

        Named exactly rather than caught loosely: a broad match here would pass on any
        exception mentioning a side, including one this stream raised for an unrelated
        reason, and the point is that the domain type is doing the refusing."""
        spots = stream()
        with pytest.raises(DomainError, match=r"crossed quote, bid 1\.158 is above ask 1\.157"):
            await spots.handle_event(spot_event(EURUSD_SYMBOL_ID, bid=115_800, ask=115_700))
