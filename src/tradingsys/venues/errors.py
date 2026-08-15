"""Venue failures, classified by what the caller should do about them.

The distinction that matters most is between a request that definitely did not happen
and one whose outcome is unknown. :class:`VenueConnectivityError` means unknown: the
order may be live at the venue. Retrying it blindly is how a position doubles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tradingsys.core.errors import TradingSysError

if TYPE_CHECKING:
    from tradingsys.core.instrument import InstrumentId

__all__ = [
    "InstrumentNotFoundError",
    "InsufficientMarginError",
    "OrderNotFoundError",
    "OrderRejectedError",
    "PositionNotFoundError",
    "UnsupportedVenueOperationError",
    "VenueAuthenticationError",
    "VenueConnectivityError",
    "VenueError",
    "VenueRateLimitError",
    "VenueResponseError",
]


class VenueError(TradingSysError):
    """Base class for every failure originating at a venue.

    Attributes:
        venue: Identifier of the venue that failed.
    """

    def __init__(self, venue: str, message: str) -> None:
        super().__init__(f"{venue}: {message}")
        self.venue = venue


class VenueConnectivityError(VenueError):
    """The venue could not be reached, or the connection dropped mid-request.

    The outcome of the request is unknown. A caller that was placing an order must
    reconcile by client order id before retrying.
    """


class VenueAuthenticationError(VenueError):
    """Credentials were rejected. Retrying will not help."""


class VenueRateLimitError(VenueError):
    """The venue refused the request for rate reasons.

    Attributes:
        retry_after_seconds: How long the venue asked us to wait, when it says.
    """

    def __init__(self, venue: str, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(venue, message)
        self.retry_after_seconds = retry_after_seconds


class VenueResponseError(VenueError):
    """The venue answered with something this system cannot interpret.

    Raised rather than guessing. A malformed price or an unknown enum value must not
    be coerced into a plausible default.
    """


class UnsupportedVenueOperationError(VenueError):
    """The venue does not offer the requested capability.

    Deterministic and permanent for this venue, so callers should branch on
    :class:`~tradingsys.venues.models.VenueCapabilities` rather than catching this in
    a hot path.
    """


class InstrumentNotFoundError(VenueError):
    """The venue does not list the requested instrument."""

    def __init__(self, venue: str, instrument_id: InstrumentId | str) -> None:
        super().__init__(venue, f"instrument {instrument_id} is not listed at this venue")
        self.instrument_id = instrument_id


class OrderNotFoundError(VenueError):
    """The venue has no record of the requested order."""

    def __init__(self, venue: str, order_id: str) -> None:
        super().__init__(venue, f"order {order_id} is unknown to this venue")
        self.order_id = order_id


class PositionNotFoundError(VenueError):
    """There is no open position matching the request."""

    def __init__(self, venue: str, instrument_id: InstrumentId | str) -> None:
        super().__init__(venue, f"no open position in {instrument_id}")
        self.instrument_id = instrument_id


class OrderRejectedError(VenueError):
    """The venue refused the order. It definitely did not reach the book.

    Attributes:
        client_order_id: The id we submitted, for reconciliation and audit.
        reason: The venue's own reason, kept verbatim rather than mapped, because the
            mapping would lose the detail an operator needs.
    """

    def __init__(self, venue: str, client_order_id: str, reason: str) -> None:
        super().__init__(venue, f"order {client_order_id} was rejected: {reason}")
        self.client_order_id = client_order_id
        self.reason = reason


class InsufficientMarginError(OrderRejectedError):
    """The order was rejected because the account cannot support it.

    A subclass of rejection because the order definitely did not reach the book, and
    separate because the response is to reduce size rather than to alert on a bug.
    """
