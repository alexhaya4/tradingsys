"""Tests for credential expiry and refresh at the venue boundary.

The forex venue's access token expires after roughly thirty days and its refresh token
rotates when used. Both facts have to survive contact with the interface, because the
failure modes are slow and expensive: a process that never refreshes authenticates
perfectly for a month and then stops, and one that refreshes without persisting a
rotated token works until its next restart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from tests.venues.conforming import ConformingExecution, ConformingMarketData
from tradingsys.core.errors import DomainError
from tradingsys.venues.base import VenueConnection
from tradingsys.venues.enums import OrderType, PositionMode, TimeInForce
from tradingsys.venues.errors import UnsupportedVenueOperationError
from tradingsys.venues.models import CredentialRefresh, VenueCapabilities

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
THIRTY_DAYS = timedelta(days=30)


def capabilities(*, expiring: bool) -> VenueCapabilities:
    return VenueCapabilities(
        venue="fxbroker",
        position_mode=PositionMode.HEDGING if expiring else PositionMode.NETTING,
        order_types=frozenset({OrderType.MARKET}),
        time_in_force=frozenset({TimeInForce.IOC}),
        granularities=frozenset(),
        candle_prices=frozenset(),
        max_candles_per_request=1,
        credentials_expire=expiring,
    )


def refresh(**overrides: object) -> CredentialRefresh:
    defaults: dict[str, object] = {
        "refreshed_at": NOW,
        "expires_at": NOW + THIRTY_DAYS,
        "access_token": SecretStr("new-access"),
        "refresh_token": SecretStr("new-refresh"),
    }
    defaults.update(overrides)
    return CredentialRefresh(**defaults)  # type: ignore[arg-type]


class TestCredentialRefreshValue:
    def test_a_rotated_refresh_token_is_flagged(self) -> None:
        # The caller must persist this before the next restart or the account locks out.
        assert refresh().rotated

    def test_an_unrotated_refresh_token_is_not_flagged(self) -> None:
        assert not refresh(refresh_token=None).rotated

    def test_timestamps_are_normalised_to_utc(self) -> None:
        tokyo = datetime(2026, 8, 15, 21, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        assert refresh(refreshed_at=tokyo, expires_at=tokyo + THIRTY_DAYS).refreshed_at == NOW

    def test_naive_timestamps_are_rejected(self) -> None:
        with pytest.raises(DomainError, match="timezone aware"):
            refresh(refreshed_at=datetime(2026, 8, 15, 12, 0))  # noqa: DTZ001

    def test_a_credential_that_is_already_dead_is_rejected(self) -> None:
        # A venue that returns an expiry at or before the refresh has handed back
        # something unusable, and accepting it would defer the failure to the next call.
        with pytest.raises(DomainError, match="dead token"):
            refresh(expires_at=NOW)

    def test_an_expiry_before_the_refresh_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="dead token"):
            refresh(expires_at=NOW - timedelta(days=1))

    def test_an_empty_access_token_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="non-empty access token"):
            refresh(access_token=SecretStr(""))

    def test_a_credential_without_an_expiry_is_allowed(self) -> None:
        assert refresh(expires_at=None).expires_at is None

    def test_the_tokens_are_secrets(self) -> None:
        # repr must not leak them into a log line or a traceback.
        assert "new-access" not in repr(refresh())
        assert "new-refresh" not in repr(refresh())


class TestRenewalTiming:
    def test_renewal_is_due_inside_the_margin(self) -> None:
        credential = refresh()
        assert credential.is_due(NOW + THIRTY_DAYS - timedelta(days=2), timedelta(days=3))

    def test_renewal_is_not_due_outside_the_margin(self) -> None:
        credential = refresh()
        assert not credential.is_due(NOW + timedelta(days=1), timedelta(days=3))

    def test_renewal_is_due_exactly_at_the_margin_boundary(self) -> None:
        credential = refresh()
        assert credential.is_due(NOW + THIRTY_DAYS - timedelta(days=3), timedelta(days=3))

    def test_renewal_is_due_after_expiry(self) -> None:
        credential = refresh()
        assert credential.is_due(NOW + THIRTY_DAYS + timedelta(days=1), timedelta(days=3))

    def test_a_credential_that_never_expires_is_never_due(self) -> None:
        credential = refresh(expires_at=None)
        assert not credential.is_due(NOW + timedelta(days=3650), timedelta(days=3))

    def test_the_margin_is_what_leaves_room_to_retry(self) -> None:
        # With no margin, renewal only becomes due once the credential is already dead,
        # and a failed renewal then cannot be retried without a human.
        credential = refresh()
        assert not credential.is_due(NOW + THIRTY_DAYS - timedelta(seconds=1), timedelta(0))
        assert credential.is_due(NOW + THIRTY_DAYS, timedelta(0))


class TestCapabilityDeclaration:
    def test_a_venue_declares_whether_its_credentials_expire(self) -> None:
        assert capabilities(expiring=True).credentials_expire
        assert not capabilities(expiring=False).credentials_expire

    def test_the_default_is_that_credentials_do_not_expire(self) -> None:
        # Static key and secret is the common case; expiry is the exception that has to
        # be declared.
        assert not VenueCapabilities.minimal("cryptoex").credentials_expire


class TestTheInterfaceContract:
    def _market_data(self, *, expiring: bool, expires_at: datetime | None) -> ConformingMarketData:
        return ConformingMarketData(
            "fxbroker", capabilities(expiring=expiring), expires_at=expires_at
        )

    def test_expiry_is_exposed(self) -> None:
        source = self._market_data(expiring=True, expires_at=NOW + THIRTY_DAYS)
        assert source.credentials_expire_at == NOW + THIRTY_DAYS

    def test_a_non_expiring_venue_reports_no_expiry(self) -> None:
        assert self._market_data(expiring=False, expires_at=None).credentials_expire_at is None

    async def test_refreshing_returns_the_new_material(self) -> None:
        source = self._market_data(expiring=True, expires_at=NOW + timedelta(days=1))
        result = await source.refresh_credentials()
        assert result.access_token.get_secret_value() == "access-1"
        assert result.rotated

    async def test_refreshing_moves_the_expiry_forward(self) -> None:
        source = self._market_data(expiring=True, expires_at=NOW + timedelta(days=1))
        before = source.credentials_expire_at
        result = await source.refresh_credentials()
        assert before is not None
        assert source.credentials_expire_at == result.expires_at
        assert source.credentials_expire_at is not None
        assert source.credentials_expire_at > before

    async def test_refreshing_early_is_permitted(self) -> None:
        # Renewing well before expiry is the intended usage, not an error.
        source = self._market_data(expiring=True, expires_at=NOW + THIRTY_DAYS)
        await source.refresh_credentials()
        assert source.refresh_calls == 1

    async def test_a_non_expiring_venue_refuses_to_refresh(self) -> None:
        source = self._market_data(expiring=False, expires_at=None)
        with pytest.raises(UnsupportedVenueOperationError, match="nothing to refresh"):
            await source.refresh_credentials()

    async def test_execution_venues_carry_the_same_contract(self) -> None:
        # Both halves of the split interface need it: the market data connection and
        # the execution connection authenticate separately.
        venue = ConformingExecution(
            "fxbroker", capabilities(expiring=True), expires_at=NOW + timedelta(days=1)
        )
        result = await venue.refresh_credentials()
        assert result.rotated
        assert venue.credentials_expire_at == result.expires_at

    def test_refresh_is_part_of_the_shared_connection_interface(self) -> None:
        assert "refresh_credentials" in VenueConnection.__abstractmethods__
        assert "credentials_expire_at" in VenueConnection.__abstractmethods__
