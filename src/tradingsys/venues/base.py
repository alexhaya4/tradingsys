"""Venue interfaces.

Two abstract base classes divide the work by trust boundary rather than by protocol:
:class:`MarketDataSource` reads, :class:`ExecutionVenue` writes. A read-only research
process depends only on the former and cannot place an order even by mistake.

**This module declares signatures only.** No adapter lives here.

These shapes were first drawn against OANDA v20 and then, before any adapter existed,
the forex venue changed to cTrader Open API. Nothing in this module had to change to
accommodate that, which is the whole return on writing it this way: the two venues
disagree about transport (HTTP streaming against a persistent protobuf socket),
authentication (a static bearer token against OAuth2 with expiring, rotating tokens),
sizing (units against lots with a contract size), and position model (netting against
hedging), and every one of those disagreements is expressed as data rather than as a
branch on a venue name. The single addition the change did prompt is credential expiry
and refresh below, which is a real property of some venues and not a cTrader detail.

No venue's field names, identifier formats, or enum values appear here. An adapter for
either venue, or for a ccxt exchange, is written against these classes without
modifying them.

Conventions every implementation must honour:

* All timestamps are timezone aware UTC, both in and out.
* All prices, sizes, and rates are Decimal. A venue that sends numeric JSON must parse
  from the raw string, never through float.
* Time ranges are half open, ``[start, end)``, and are interpreted in UTC.
* Methods raise the exceptions in :mod:`tradingsys.venues.errors`, never a library
  specific exception from the underlying HTTP or websocket client.
* Read methods are idempotent and safe to retry. :meth:`ExecutionVenue.place_order` is
  idempotent on ``client_order_id``: resubmitting one already accepted returns the
  existing order rather than creating a second.
* Streams are async iterators that reconnect internally and raise only when recovery
  is impossible. A consumer that iterates to exhaustion has lost the connection for
  good.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from datetime import datetime
    from decimal import Decimal
    from types import TracebackType

    from tradingsys.core.instrument import Instrument, InstrumentId
    from tradingsys.venues.enums import CandlePrice, Granularity
    from tradingsys.venues.models import (
        AccountSnapshot,
        Candle,
        CredentialRefresh,
        ExecutionEvent,
        Fill,
        FundingRate,
        Order,
        OrderBook,
        OrderRequest,
        Position,
        Quote,
        SwapRates,
        Trade,
        VenueCapabilities,
    )

__all__ = ["ExecutionVenue", "MarketDataSource", "VenueConnection"]


class VenueConnection(ABC):
    """Lifecycle shared by both venue interfaces.

    Connections are opened explicitly rather than on first use, so that a
    misconfigured credential fails at startup instead of at the first order.
    """

    @property
    @abstractmethod
    def venue(self) -> str:
        """Stable identifier of this venue, matching the ``venue`` on its instruments."""
        raise NotImplementedError

    @property
    @abstractmethod
    def capabilities(self) -> VenueCapabilities:
        """What this venue supports. Must be truthful; callers branch on it."""
        raise NotImplementedError

    @abstractmethod
    async def connect(self) -> None:
        """Establish connectivity and verify credentials.

        Must be safe to call more than once. Implementations should perform a real
        round trip, not merely construct a client, so that a bad token is discovered
        here.

        Raises:
            VenueAuthenticationError: Credentials were rejected.
            VenueConnectivityError: The venue is unreachable.
        """
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Release all connections and background tasks.

        Must be safe to call when never connected, and must not raise on a connection
        that has already failed: shutdown paths call this in a finally block.
        """
        raise NotImplementedError

    @abstractmethod
    async def ping(self) -> float:
        """Round trip time to the venue in seconds, for health checks.

        Raises:
            VenueConnectivityError: The venue did not respond.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # credentials
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def credentials_expire_at(self) -> datetime | None:
        """When the current credential stops working, or ``None`` if it does not expire.

        A static key and secret returns ``None`` forever. An OAuth style venue returns
        the access token's expiry, which for cTrader is roughly thirty days out. A
        supervisor polls this rather than tracking venue-specific token lifetimes, and
        :attr:`~tradingsys.venues.models.VenueCapabilities.credentials_expire` says in
        advance whether it is worth polling at all.
        """
        raise NotImplementedError

    @abstractmethod
    async def refresh_credentials(self) -> CredentialRefresh:
        """Renew the credential and return the new material.

        Implementations must not quietly keep the result to themselves. Where the venue
        rotates the refresh credential, the caller has to persist the replacement before
        the process restarts, and only the caller knows where its secrets live. An
        adapter that refreshed itself in memory would work until the first restart after
        a rotation and then fail authentication with no obvious cause.

        Must be safe to call before expiry: this is how a supervisor renews early, and
        renewing early is the point, because a renewal that fails after expiry cannot be
        retried without a human reauthorising the application.

        Raises:
            UnsupportedVenueOperationError: The venue's credentials do not expire, so
                there is nothing to refresh. Check
                :attr:`~tradingsys.venues.models.VenueCapabilities.credentials_expire`
                first.
            VenueAuthenticationError: The venue refused the refresh. The credential is
                now unrecoverable without manual reauthorisation, so this must page
                someone rather than be retried indefinitely.
            VenueConnectivityError: The venue could not be reached. Retryable.
        """
        raise NotImplementedError

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


class MarketDataSource(VenueConnection):
    """Read-only access to a venue's instruments, prices, history, and carry rates."""

    # ------------------------------------------------------------------
    # instruments
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch_instruments(self) -> Sequence[Instrument]:
        """Every instrument this venue offers, with its live trading rules.

        Implementations translate the venue's own metadata into
        :class:`~tradingsys.core.instrument.Instrument`, including tick size, step
        size, minimum and maximum size, leverage cap, financing convention, and
        trading schedule. Values must reflect what the venue reports now, not a
        cached snapshot from deployment time: exchanges change step sizes without
        notice.

        Raises:
            VenueConnectivityError: The venue is unreachable.
            VenueResponseError: The response could not be interpreted.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_instrument(self, instrument_id: InstrumentId) -> Instrument:
        """One instrument's definition.

        Raises:
            InstrumentNotFoundError: The venue does not list this instrument.
        """
        raise NotImplementedError

    @abstractmethod
    async def server_time(self) -> datetime:
        """The venue's own clock, in UTC.

        Used to measure clock skew. A venue that rejects orders with a stale timestamp
        makes this a precondition for trading, not a diagnostic.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # current prices
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch_quote(self, instrument_id: InstrumentId) -> Quote:
        """The current top of book.

        Raises:
            InstrumentNotFoundError: The venue does not list this instrument.
            VenueConnectivityError: The venue is unreachable.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_quotes(self, instrument_ids: Sequence[InstrumentId]) -> Sequence[Quote]:
        """Current top of book for several instruments in one round trip.

        Returned in the order requested. An instrument the venue cannot price is
        omitted rather than represented by a placeholder, so callers must match on
        :attr:`~tradingsys.venues.models.Quote.instrument_id` rather than by position.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_order_book(self, instrument_id: InstrumentId, depth: int) -> OrderBook:
        """A depth snapshot.

        Args:
            instrument_id: Instrument to snapshot.
            depth: Number of levels per side to request. The venue may return fewer.

        Raises:
            UnsupportedVenueOperationError: The venue publishes no depth, which is
                normal for a forex broker quoting a single price.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch_candles(
        self,
        instrument_id: InstrumentId,
        granularity: Granularity,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        price: CandlePrice,
        include_incomplete: bool = False,
    ) -> Sequence[Candle]:
        """Historical bars, oldest first.

        Args:
            instrument_id: Instrument to fetch.
            granularity: Bar width.
            start: Inclusive left edge of the range, UTC. When omitted, the venue's
                own default window applies, so callers that care must pass it.
            end: Exclusive right edge, UTC.
            limit: Maximum bars to return. Implementations must not exceed
                :attr:`~tradingsys.venues.models.VenueCapabilities.max_candles_per_request`
                in a single call, and must page internally when the range needs it.
            price: Which price series to build bars from. Forex brokers offer bid, ask,
                and mid separately; exchanges offer traded prices.
            include_incomplete: Whether to include the bar currently forming. It is
                excluded by default because acting on a partial bar is almost always a
                bug.

        Returns:
            Bars in ascending time order with no duplicates. Gaps are real: a venue
            that was closed produces no bar, and implementations must not synthesise
            one.

        Raises:
            UnsupportedVenueOperationError: The venue cannot serve this granularity or
                price component.
            VenueRateLimitError: The venue refused the request for rate reasons.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_trades(
        self,
        instrument_id: InstrumentId,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[Trade]:
        """Historical public trades, oldest first.

        Raises:
            UnsupportedVenueOperationError: The venue publishes no trade tape, which
                is the normal case for a forex broker.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------

    @abstractmethod
    def stream_quotes(self, instrument_ids: Sequence[InstrumentId]) -> AsyncIterator[Quote]:
        """A live quote stream for the given instruments.

        The iterator owns its transport: it reconnects on a dropped connection, honours
        the venue's heartbeat, and resumes without the caller noticing. It ends only
        when the caller stops consuming or the connection cannot be recovered, in which
        case it raises rather than returning quietly.

        Implementations must not buffer without bound. A consumer that falls behind
        should receive the most recent quotes, since a stale price is worthless.

        Raises:
            UnsupportedVenueOperationError: The venue offers no price stream.
            VenueConnectivityError: The stream could not be recovered.
        """
        raise NotImplementedError

    @abstractmethod
    def stream_trades(self, instrument_ids: Sequence[InstrumentId]) -> AsyncIterator[Trade]:
        """A live public trade stream.

        Raises:
            UnsupportedVenueOperationError: The venue publishes no trade tape.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # carry
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch_funding_rate(self, instrument_id: InstrumentId) -> FundingRate:
        """The current funding rate for a perpetual style instrument.

        Raises:
            UnsupportedVenueOperationError: The instrument is not funded periodically.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_swap_rates(self, instrument_id: InstrumentId) -> SwapRates:
        """Current overnight swap rates for a forex style instrument.

        Raises:
            UnsupportedVenueOperationError: The instrument does not use daily swaps.
        """
        raise NotImplementedError


class ExecutionVenue(VenueConnection):
    """Order entry and account state.

    Implementations are responsible for idempotency and for translating venue errors
    into this system's exception types. They must not retry a submission blindly: a
    retry is only safe once the venue has been queried by ``client_order_id``.
    """

    # ------------------------------------------------------------------
    # account
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch_account(self) -> AccountSnapshot:
        """Current balance, equity, and margin.

        Raises:
            VenueConnectivityError: The venue is unreachable.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_positions(self) -> Sequence[Position]:
        """All open positions.

        On a netting venue there is at most one position per instrument. On a hedging
        venue there may be several, distinguished by
        :attr:`~tradingsys.venues.models.Position.position_id`. Callers must consult
        :attr:`~tradingsys.venues.models.VenueCapabilities.position_mode` rather than
        assuming.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_position(self, instrument_id: InstrumentId) -> Position | None:
        """The open position in one instrument, or ``None`` if flat.

        Raises:
            UnsupportedVenueOperationError: The venue is in hedging mode, where an
                instrument may have several positions and this question is ambiguous.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> Order:
        """Submit an order and return the venue's acknowledgement.

        Idempotent on ``client_order_id``. If the venue has already accepted that id,
        implementations must return the existing order rather than submitting a
        second: a lost acknowledgement must never become a duplicate position.

        The returned order reflects the venue's state at acknowledgement, which for a
        market order may already be filled and for a resting order will be open. It is
        not a promise of a fill.

        Raises:
            OrderRejectedError: The venue refused the order, with its reason.
            InsufficientMarginError: The account cannot support the order.
            UnsupportedVenueOperationError: The request uses a feature this venue does
                not have. Callers can check first with
                :meth:`~tradingsys.venues.models.VenueCapabilities.supports`.
            VenueConnectivityError: The outcome is unknown. Callers must reconcile by
                ``client_order_id`` before retrying.
        """
        raise NotImplementedError

    @abstractmethod
    async def cancel_order(self, venue_order_id: str) -> Order:
        """Cancel a working order and return its resulting state.

        Cancelling an order that is already terminal is not an error: the current state
        is returned, because a race between a fill and a cancel is normal and must not
        be reported as a failure.

        Raises:
            OrderNotFoundError: The venue does not know this order.
        """
        raise NotImplementedError

    @abstractmethod
    async def modify_order(
        self,
        venue_order_id: str,
        *,
        quantity: Decimal | None = None,
        limit_price: Decimal | None = None,
        trigger_price: Decimal | None = None,
        expires_at: datetime | None = None,
    ) -> Order:
        """Amend a working order in place.

        Only the arguments supplied are changed. Venues that implement amendment as
        cancel-and-replace must say so through
        :attr:`~tradingsys.venues.models.VenueCapabilities.supports_order_modification`,
        because the replacement loses queue priority and may partially fill in between.

        Raises:
            UnsupportedVenueOperationError: The venue cannot amend orders.
            OrderNotFoundError: The venue does not know this order.
            OrderRejectedError: The amendment was refused.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_order(self, venue_order_id: str) -> Order:
        """One order's current state.

        Raises:
            OrderNotFoundError: The venue does not know this order.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_order_by_client_id(self, client_order_id: str) -> Order | None:
        """Look an order up by the id we assigned, or ``None`` if the venue never saw it.

        This is the reconciliation primitive: after a lost acknowledgement, it answers
        whether the order exists before any retry.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_open_orders(self, instrument_id: InstrumentId | None = None) -> Sequence[Order]:
        """Every working order, optionally filtered to one instrument."""
        raise NotImplementedError

    @abstractmethod
    async def fetch_fills(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        instrument_id: InstrumentId | None = None,
        limit: int | None = None,
    ) -> Sequence[Fill]:
        """Historical executions, oldest first, over a half open UTC range."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # positions
    # ------------------------------------------------------------------

    @abstractmethod
    async def close_position(
        self,
        instrument_id: InstrumentId,
        *,
        quantity: Decimal | None = None,
        position_id: str | None = None,
    ) -> Order:
        """Flatten a position, or reduce it by ``quantity``.

        Args:
            instrument_id: Instrument to close.
            quantity: Amount to close in the instrument's quantity unit. ``None``
                closes the whole position.
            position_id: Which position to close on a hedging venue. Required there,
                and refused on a netting venue where it has no meaning.

        Raises:
            PositionNotFoundError: There is no such open position.
            OrderRejectedError: The closing order was refused.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------

    @abstractmethod
    def stream_execution_events(self) -> AsyncIterator[ExecutionEvent]:
        """A live stream of order, fill, position, and account changes.

        The same reconnection contract as the market data streams. Implementations
        that receive a venue sequence number must surface it on the event so that a
        gap after reconnection is detectable, and must reconcile by fetching current
        state rather than assuming the missed events were unimportant.

        Raises:
            UnsupportedVenueOperationError: The venue offers no execution stream, in
                which case callers must poll.
            VenueConnectivityError: The stream could not be recovered.
        """
        raise NotImplementedError
