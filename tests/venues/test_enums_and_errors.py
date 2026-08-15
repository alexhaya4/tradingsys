"""Tests for venue enumerations and the error hierarchy.

These cover the small pieces of behaviour that hang off the enums and exceptions.
Neither is decorative: ``OrderSide.sign`` turns a size into a signed exposure, and the
attributes on the exceptions are what a retry policy and an operator alert read.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tradingsys.core.errors import DomainError, TradingSysError
from tradingsys.core.instrument import InstrumentId
from tradingsys.venues.enums import Granularity, OrderSide
from tradingsys.venues.errors import (
    InstrumentNotFoundError,
    InsufficientMarginError,
    OrderNotFoundError,
    OrderRejectedError,
    PositionNotFoundError,
    UnsupportedVenueOperationError,
    VenueAuthenticationError,
    VenueConnectivityError,
    VenueError,
    VenueRateLimitError,
    VenueResponseError,
)


class TestOrderSide:
    def test_opposite(self) -> None:
        assert OrderSide.BUY.opposite is OrderSide.SELL
        assert OrderSide.SELL.opposite is OrderSide.BUY

    def test_opposite_is_an_involution(self) -> None:
        for side in OrderSide:
            assert side.opposite.opposite is side

    def test_sign_turns_a_size_into_a_signed_exposure(self) -> None:
        assert OrderSide.BUY.sign == 1
        assert OrderSide.SELL.sign == -1
        assert 1000 * OrderSide.SELL.sign == -1000

    def test_the_signs_cancel(self) -> None:
        assert OrderSide.BUY.sign + OrderSide.SELL.sign == 0


class TestGranularity:
    def test_durations_are_exact(self) -> None:
        assert Granularity.M1.duration == timedelta(minutes=1)
        assert Granularity.H4.duration == timedelta(hours=4)
        assert Granularity.W1.duration == timedelta(days=7)

    def test_seconds_match_the_duration(self) -> None:
        for granularity in Granularity:
            assert granularity.seconds == int(granularity.duration.total_seconds())

    def test_every_member_has_a_positive_duration(self) -> None:
        for granularity in Granularity:
            assert granularity.duration > timedelta(0)

    def test_durations_are_strictly_increasing_in_declaration_order(self) -> None:
        widths = [member.duration for member in Granularity]
        assert widths == sorted(widths)
        assert len(set(widths)) == len(widths)

    def test_from_duration_round_trips(self) -> None:
        for granularity in Granularity:
            assert Granularity.from_duration(granularity.duration) is granularity

    def test_from_duration_rejects_an_unsupported_width(self) -> None:
        with pytest.raises(DomainError, match="no granularity of width"):
            Granularity.from_duration(timedelta(minutes=7))

    def test_the_error_lists_what_is_supported(self) -> None:
        with pytest.raises(DomainError) as caught:
            Granularity.from_duration(timedelta(seconds=3))
        assert "1m" in str(caught.value)

    def test_a_month_is_not_a_granularity(self) -> None:
        # Calendar months vary in length, so bar arithmetic over them is ambiguous and
        # the enum deliberately stops at a week.
        with pytest.raises(DomainError):
            Granularity.from_duration(timedelta(days=30))


class TestErrorHierarchy:
    def test_every_venue_error_is_a_tradingsys_error(self) -> None:
        # One except clause at the top of a supervision loop must catch all of them.
        for error in (
            VenueConnectivityError("v", "x"),
            VenueAuthenticationError("v", "x"),
            VenueResponseError("v", "x"),
            UnsupportedVenueOperationError("v", "x"),
        ):
            assert isinstance(error, VenueError)
            assert isinstance(error, TradingSysError)

    def test_the_venue_is_carried_and_prefixed(self) -> None:
        error = VenueConnectivityError("cryptoex", "socket closed")
        assert error.venue == "cryptoex"
        assert str(error) == "cryptoex: socket closed"


class TestRateLimiting:
    def test_a_retry_delay_is_carried_when_the_venue_supplies_one(self) -> None:
        error = VenueRateLimitError("cryptoex", "too many requests", retry_after_seconds=2.5)
        assert error.retry_after_seconds == 2.5

    def test_the_delay_is_absent_when_the_venue_does_not_say(self) -> None:
        assert VenueRateLimitError("cryptoex", "slow down").retry_after_seconds is None


class TestNotFoundErrors:
    def test_an_unlisted_instrument_names_it(self) -> None:
        error = InstrumentNotFoundError("fxbroker", InstrumentId("fxbroker", "EUR/USD"))
        assert "fxbroker:EUR/USD" in str(error)
        assert error.instrument_id == InstrumentId("fxbroker", "EUR/USD")

    def test_an_unknown_order_names_it(self) -> None:
        error = OrderNotFoundError("fxbroker", "v-9")
        assert error.order_id == "v-9"
        assert "v-9" in str(error)

    def test_a_missing_position_names_the_instrument(self) -> None:
        error = PositionNotFoundError("fxbroker", "fxbroker:EUR/USD")
        assert error.instrument_id == "fxbroker:EUR/USD"
        assert "no open position" in str(error)


class TestRejection:
    def test_a_rejection_carries_the_client_id_and_the_venue_reason(self) -> None:
        # The client order id is what reconciliation looks the order up by, and the
        # reason is kept verbatim because mapping it would lose what an operator needs.
        error = OrderRejectedError("fxbroker", "c-1", "market is closed")
        assert error.client_order_id == "c-1"
        assert error.reason == "market is closed"
        assert "c-1" in str(error)
        assert "market is closed" in str(error)

    def test_insufficient_margin_is_a_rejection(self) -> None:
        # Subclassing matters: it means the order definitely did not reach the book, so
        # a caller that handles rejections handles this too, without knowing about it.
        error = InsufficientMarginError("fxbroker", "c-2", "not enough free margin")
        assert isinstance(error, OrderRejectedError)
        assert error.client_order_id == "c-2"

    def test_a_rejection_is_distinguishable_from_an_unknown_outcome(self) -> None:
        # This distinction is the one that matters most: a rejection is safe to retry
        # because nothing reached the venue, a connectivity failure is not.
        assert not isinstance(OrderRejectedError("v", "c", "why"), VenueConnectivityError)
        assert not isinstance(VenueConnectivityError("v", "x"), OrderRejectedError)
