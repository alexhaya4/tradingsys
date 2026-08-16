"""Decoding Bybit's ``orderbook.1`` stream into top of book quotes.

Observed behaviour, from an hour of the live linear stream on 2026-08-16: at depth 1
every message arrives as ``type: "snapshot"``, one price level per side, with ``u``
incrementing by exactly one per message. No deltas were seen at all.

The delta path is implemented anyway, because the documentation describes it and a
venue is entitled to start sending what it documents. What is not done is assuming the
observed shape: a message that fails to fit raises rather than being coerced, since a
book that has silently drifted from the venue's is worse than no book.

Three rules come from Bybit's own documentation and are implemented literally:

* ``type: "snapshot"`` replaces the book.
* In a delta, a size of ``"0"`` deletes that price level.
* ``u == 1`` means the service restarted and the message is a snapshot regardless of
  its type field.

One rule is ours. A delta that arrives before any snapshot cannot be applied to
anything, so it raises :class:`ResyncRequiredError` and the caller resubscribes. Applying it
to an empty book would produce a half book that looks like a real one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, final

from tradingsys.core.errors import DomainError
from tradingsys.core.numeric import to_decimal
from tradingsys.venues.bybit.instruments import VENUE
from tradingsys.venues.errors import VenueResponseError
from tradingsys.venues.models import Quote

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from tradingsys.core.instrument import InstrumentId

__all__ = [
    "ORDERBOOK_TOPIC_PREFIX",
    "BookState",
    "ResyncRequiredError",
    "topic_for",
]

ORDERBOOK_TOPIC_PREFIX: Final = "orderbook.1."
"""Depth one. Deeper books cost bandwidth we have no use for: the system records the
spread it would pay, and that is the top of the book."""

_RESTART_UPDATE_ID: Final = 1
"""Bybit reuses ``u == 1`` to signal that the service restarted."""

_LEVEL_FIELDS: Final = 2
"""A book level is exactly ``[price, size]``, both as decimal strings."""


class ResyncRequiredError(VenueResponseError):
    """The local book cannot be trusted and the subscription must be restarted.

    Separate from an ordinary response error because the response was not malformed:
    the connection simply missed something, and the fix is to resubscribe rather than
    to alert anyone.
    """


def topic_for(venue_symbol: str) -> str:
    return f"{ORDERBOOK_TOPIC_PREFIX}{venue_symbol}"


@final
@dataclass(slots=True)
class BookState:
    """The top of one instrument's book, rebuilt from the stream.

    Mutable by nature: it is the running state of a subscription. One instance per
    topic, discarded whenever the connection drops, because a book carried across a
    reconnect is a book with an unknown number of missed updates in it.
    """

    instrument_id: InstrumentId
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    last_update_id: int | None = None
    have_snapshot: bool = False

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_update_id = None
        self.have_snapshot = False

    def apply(self, message: Mapping[str, Any]) -> Quote | None:
        """Apply one stream message and return the resulting quote.

        Returns:
            The new top of book, or ``None`` when the message leaves one side empty,
            which is a real state at depth one and not something to invent a price for.

        Raises:
            ResyncRequiredError: A delta arrived with no snapshot to apply it to, or the
                update id moved backwards.
            VenueResponseError: The message is not shaped like an orderbook message.
        """
        data = message.get("data")
        if not isinstance(data, dict):
            raise VenueResponseError(VENUE, f"orderbook message has no data object: {message!r}")
        update_id = _int(data, "u")
        kind = message.get("type")
        is_snapshot = kind == "snapshot" or update_id == _RESTART_UPDATE_ID
        if kind not in ("snapshot", "delta"):
            raise VenueResponseError(VENUE, f"unknown orderbook message type {kind!r}")

        if is_snapshot:
            self.bids.clear()
            self.asks.clear()
            self.have_snapshot = True
        elif not self.have_snapshot:
            raise ResyncRequiredError(
                VENUE,
                f"{self.instrument_id}: a delta arrived before any snapshot, so there is "
                f"no book to apply it to",
            )
        elif self.last_update_id is not None and update_id < self.last_update_id:
            raise ResyncRequiredError(
                VENUE,
                f"{self.instrument_id}: update id went backwards, {update_id} after "
                f"{self.last_update_id}",
            )

        _apply_levels(self.bids, _levels(data, "b"))
        _apply_levels(self.asks, _levels(data, "a"))
        self.last_update_id = update_id

        if not self.bids or not self.asks:
            return None
        best_bid = max(self.bids)
        best_ask = min(self.asks)
        try:
            return Quote(
                instrument_id=self.instrument_id,
                ts=_event_time(message),
                bid=best_bid,
                ask=best_ask,
                bid_size=self.bids[best_bid],
                ask_size=self.asks[best_ask],
            )
        except DomainError as error:
            # A crossed top of book on a matched exchange is a venue anomaly, not a
            # bug here. It is reported and the message dropped, because the recorder
            # dying on one impossible quote would cost every subsequent one.
            raise VenueResponseError(
                VENUE, f"{self.instrument_id}: refusing an unusable quote, {error}"
            ) from error


def _event_time(message: Mapping[str, Any]) -> datetime:
    """When the market event happened, preferring the matching engine's own clock.

    ``cts`` is stamped by the matching engine and ``ts`` when the message was pushed;
    they differ by a couple of milliseconds. The engine's time is the one that lines
    these quotes up against trades and against any other venue, so it wins when
    present.
    """
    raw = message.get("cts", message.get("ts"))
    if not isinstance(raw, int):
        raise VenueResponseError(VENUE, f"orderbook message has no usable timestamp: {raw!r}")
    return datetime.fromtimestamp(raw / 1000, tz=UTC)


def _levels(data: Mapping[str, Any], name: str) -> Sequence[Sequence[str]]:
    levels = data.get(name)
    if not isinstance(levels, list):
        raise VenueResponseError(VENUE, f"orderbook side {name!r} is missing or not a list")
    for level in levels:
        if (
            not isinstance(level, list)
            or len(level) != _LEVEL_FIELDS
            or not all(isinstance(part, str) for part in level)
        ):
            raise VenueResponseError(
                VENUE, f"orderbook side {name!r} has a malformed level: {level!r}"
            )
    return levels


def _apply_levels(side: dict[Decimal, Decimal], levels: Sequence[Sequence[str]]) -> None:
    for raw_price, raw_size in levels:
        price = to_decimal(raw_price, what="price")
        size = to_decimal(raw_size, what="size")
        if size == 0:
            # Documented as a deletion. Storing a zero sized level instead would leave
            # a price in the book that nobody is quoting.
            side.pop(price, None)
        else:
            side[price] = size


def _int(data: Mapping[str, Any], name: str) -> int:
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise VenueResponseError(VENUE, f"orderbook field {name!r} is missing or not an integer")
    return value
