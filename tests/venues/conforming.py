"""A minimal conforming implementation of the venue interfaces, for contract tests.

This is not a simulated venue and must never be used as one. It exists to prove three
things about the abstract base classes themselves:

1. Both interfaces can be implemented at all, with the declared signatures.
2. Every abstract method is actually abstract, so a partial implementation cannot be
   instantiated.
3. The async context manager on the base class drives connect and close.

Every method returns a value recorded by the test that set it up, or raises. Nothing
here invents market behaviour.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, final

from pydantic import SecretStr

from tradingsys.core.clock import utc_now
from tradingsys.venues.base import ExecutionVenue, MarketDataSource
from tradingsys.venues.errors import (
    InstrumentNotFoundError,
    OrderNotFoundError,
    PositionNotFoundError,
    UnsupportedVenueOperationError,
)
from tradingsys.venues.models import CredentialRefresh

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Sequence
    from datetime import datetime
    from decimal import Decimal

    from tradingsys.core.instrument import Instrument, InstrumentId
    from tradingsys.venues.enums import CandlePrice, Granularity
    from tradingsys.venues.models import (
        AccountSnapshot,
        Candle,
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


class _Connection:
    """Shared lifecycle bookkeeping for the two doubles below."""

    def __init__(
        self,
        venue: str,
        capabilities: VenueCapabilities,
        expires_at: datetime | None = None,
    ) -> None:
        self._venue = venue
        self._capabilities = capabilities
        self._expires_at = expires_at
        self.connect_calls = 0
        self.close_calls = 0
        self.refresh_calls = 0

    @property
    def venue(self) -> str:
        return self._venue

    @property
    def capabilities(self) -> VenueCapabilities:
        return self._capabilities

    async def connect(self) -> None:
        self.connect_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    async def ping(self) -> float:
        return 0.0

    @property
    def credentials_expire_at(self) -> datetime | None:
        return self._expires_at

    async def refresh_credentials(self) -> CredentialRefresh:
        if not self._capabilities.credentials_expire:
            raise UnsupportedVenueOperationError(
                self.venue, "these credentials do not expire, so there is nothing to refresh"
            )
        self.refresh_calls += 1
        now = utc_now()
        self._expires_at = now + timedelta(days=30)
        return CredentialRefresh(
            refreshed_at=now,
            expires_at=self._expires_at,
            access_token=SecretStr(f"access-{self.refresh_calls}"),
            refresh_token=SecretStr(f"refresh-{self.refresh_calls}"),
        )


@final
class ConformingMarketData(_Connection, MarketDataSource):
    """A market data source that returns exactly what it was given."""

    def __init__(
        self,
        venue: str,
        capabilities: VenueCapabilities,
        *,
        instruments: Sequence[Instrument] = (),
        quotes: Sequence[Quote] = (),
        candles: Sequence[Candle] = (),
        server_now: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        super().__init__(venue, capabilities, expires_at)
        self._instruments = tuple(instruments)
        self._quotes = tuple(quotes)
        self._candles = tuple(candles)
        self._server_now = server_now
        self.candle_requests: list[tuple[InstrumentId, Granularity, CandlePrice]] = []

    async def fetch_instruments(self) -> Sequence[Instrument]:
        return self._instruments

    async def fetch_instrument(self, instrument_id: InstrumentId) -> Instrument:
        for instrument in self._instruments:
            if instrument.id == instrument_id:
                return instrument
        raise InstrumentNotFoundError(self.venue, instrument_id)

    async def server_time(self) -> datetime:
        if self._server_now is None:
            raise UnsupportedVenueOperationError(self.venue, "no server clock was configured")
        return self._server_now

    async def fetch_quote(self, instrument_id: InstrumentId) -> Quote:
        for quote in self._quotes:
            if quote.instrument_id == instrument_id:
                return quote
        raise InstrumentNotFoundError(self.venue, instrument_id)

    async def fetch_quotes(self, instrument_ids: Sequence[InstrumentId]) -> Sequence[Quote]:
        wanted = set(instrument_ids)
        return tuple(quote for quote in self._quotes if quote.instrument_id in wanted)

    async def fetch_order_book(self, instrument_id: InstrumentId, depth: int) -> OrderBook:
        raise UnsupportedVenueOperationError(
            self.venue, f"no order book for {instrument_id} at depth {depth}"
        )

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
        self.capabilities.require_granularity(granularity, price)
        self.candle_requests.append((instrument_id, granularity, price))
        selected = [
            candle
            for candle in self._candles
            if candle.instrument_id == instrument_id
            and candle.granularity is granularity
            and candle.price is price
            and (start is None or candle.start >= start)
            and (end is None or candle.start < end)
            and (include_incomplete or candle.complete)
        ]
        return tuple(selected[:limit] if limit is not None else selected)

    async def fetch_trades(
        self,
        instrument_id: InstrumentId,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[Trade]:
        raise UnsupportedVenueOperationError(self.venue, f"no trade tape for {instrument_id}")

    def stream_quotes(self, instrument_ids: Sequence[InstrumentId]) -> AsyncIterator[Quote]:
        wanted = set(instrument_ids)
        return _iterate(quote for quote in self._quotes if quote.instrument_id in wanted)

    def stream_trades(self, instrument_ids: Sequence[InstrumentId]) -> AsyncIterator[Trade]:
        raise UnsupportedVenueOperationError(
            self.venue, f"no trade stream for {len(instrument_ids)} instruments"
        )

    async def fetch_funding_rate(self, instrument_id: InstrumentId) -> FundingRate:
        raise UnsupportedVenueOperationError(self.venue, f"{instrument_id} is not funded")

    async def fetch_swap_rates(self, instrument_id: InstrumentId) -> SwapRates:
        raise UnsupportedVenueOperationError(self.venue, f"{instrument_id} has no daily swap")


@final
class ConformingExecution(_Connection, ExecutionVenue):
    """An execution venue that records requests and returns prepared orders."""

    def __init__(
        self,
        venue: str,
        capabilities: VenueCapabilities,
        *,
        account: AccountSnapshot | None = None,
        positions: Sequence[Position] = (),
        expires_at: datetime | None = None,
    ) -> None:
        super().__init__(venue, capabilities, expires_at)
        self._account = account
        self._positions = tuple(positions)
        self.submitted: list[OrderRequest] = []
        self.orders: dict[str, Order] = {}

    async def fetch_account(self) -> AccountSnapshot:
        if self._account is None:
            raise UnsupportedVenueOperationError(self.venue, "no account was configured")
        return self._account

    async def fetch_positions(self) -> Sequence[Position]:
        return self._positions

    async def fetch_position(self, instrument_id: InstrumentId) -> Position | None:
        for position in self._positions:
            if position.instrument_id == instrument_id:
                return position
        return None

    async def place_order(self, request: OrderRequest) -> Order:
        self.capabilities.require_supported(request)
        existing = await self.fetch_order_by_client_id(request.client_order_id)
        if existing is not None:
            return existing
        self.submitted.append(request)
        raise UnsupportedVenueOperationError(
            self.venue, "this double records requests but does not simulate acknowledgements"
        )

    async def cancel_order(self, venue_order_id: str) -> Order:
        return self._require_order(venue_order_id)

    async def modify_order(
        self,
        venue_order_id: str,
        *,
        quantity: Decimal | None = None,
        limit_price: Decimal | None = None,
        trigger_price: Decimal | None = None,
        expires_at: datetime | None = None,
    ) -> Order:
        raise UnsupportedVenueOperationError(
            self.venue, f"order {venue_order_id} cannot be amended"
        )

    async def fetch_order(self, venue_order_id: str) -> Order:
        return self._require_order(venue_order_id)

    async def fetch_order_by_client_id(self, client_order_id: str) -> Order | None:
        for order in self.orders.values():
            if order.client_order_id == client_order_id:
                return order
        return None

    async def fetch_open_orders(self, instrument_id: InstrumentId | None = None) -> Sequence[Order]:
        return tuple(
            order
            for order in self.orders.values()
            if order.is_working and (instrument_id is None or order.instrument_id == instrument_id)
        )

    async def fetch_fills(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        instrument_id: InstrumentId | None = None,
        limit: int | None = None,
    ) -> Sequence[Fill]:
        return ()

    async def close_position(
        self,
        instrument_id: InstrumentId,
        *,
        quantity: Decimal | None = None,
        position_id: str | None = None,
    ) -> Order:
        raise PositionNotFoundError(self.venue, instrument_id)

    def stream_execution_events(self) -> AsyncIterator[ExecutionEvent]:
        raise UnsupportedVenueOperationError(self.venue, "no execution stream")

    def _require_order(self, venue_order_id: str) -> Order:
        try:
            return self.orders[venue_order_id]
        except KeyError:
            raise OrderNotFoundError(self.venue, venue_order_id) from None


async def _iterate[T](values: Iterable[T]) -> AsyncIterator[T]:
    """Turn a synchronous iterable into an async iterator."""
    for value in values:
        yield value


class PartialMarketData(MarketDataSource):
    """Deliberately incomplete: used to assert that the ABC refuses instantiation."""

    @property
    def venue(self) -> str:
        return "partial"

    @property
    def capabilities(self) -> VenueCapabilities:  # pragma: no cover - never instantiated
        raise NotImplementedError
