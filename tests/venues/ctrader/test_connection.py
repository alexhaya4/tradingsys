"""Transport behaviour: the handshake, the live flag assertion, and death detection.

The refusals carry more weight here than the accept. A handshake that succeeds is
exercised by every other test in this file as a precondition; a handshake that lets a
demo configuration authorise a live account is the failure this layer exists to
prevent, and it has to be proven to refuse rather than assumed to.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.venues.ctrader.scripted_venue import ScriptedAccount, ScriptedVenue
from tradingsys.config.settings import ForexVenueSettings, VenueEnvironment
from tradingsys.core.errors import TradingSysError
from tradingsys.venues.ctrader.connection import (
    ACCOUNT_AUTH_REQ,
    APPLICATION_AUTH_REQ,
    GET_ACCOUNTS_REQ,
    CTraderConnection,
)
from tradingsys.venues.ctrader.framing import envelope_for
from tradingsys.venues.ctrader.messages.OpenApiCommonMessages_pb2 import ProtoMessage
from tradingsys.venues.ctrader.messages.OpenApiMessages_pb2 import ProtoOASpotEvent
from tradingsys.venues.ctrader.spots import SPOT_EVENT
from tradingsys.venues.errors import VenueAuthenticationError, VenueConnectivityError

pytestmark = pytest.mark.asyncio

DEMO_ACCOUNT = ScriptedAccount(
    ctid_trader_account_id=42_000_001, trader_login=5_325_402, is_live=False
)
LIVE_ACCOUNT = ScriptedAccount(
    ctid_trader_account_id=42_000_002, trader_login=5_325_402, is_live=True
)
FLAGLESS_ACCOUNT = ScriptedAccount(
    ctid_trader_account_id=42_000_003, trader_login=5_325_402, is_live=None
)


def settings(
    *,
    environment: VenueEnvironment = VenueEnvironment.PRACTICE,
    account_id: str | None = "5325402",
    heartbeat_interval_seconds: float = 60.0,
    stream_read_timeout_seconds: float = 60.0,
    request_timeout_seconds: float = 5.0,
) -> ForexVenueSettings:
    """A settings object for the transport, with the timings a test needs to control.

    Built through the real model rather than a stand in, so a field renamed in
    configuration breaks these tests rather than letting them drift.
    """
    return ForexVenueSettings(
        enabled=True,
        environment=environment,
        demo_api_host="demo.ctraderapi.com",
        live_api_host="live.ctraderapi.com",
        api_port=5035,
        token_url="https://openapi.ctrader.com/apps/token",
        request_timeout_seconds=request_timeout_seconds,
        stream_read_timeout_seconds=stream_read_timeout_seconds,
        max_retries=3,
        retry_backoff_seconds=0.5,
        max_requests_per_second=30.0,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        token_refresh_margin_seconds=259200.0,
        account_id=account_id,
        client_id="a-client-id",
        client_secret="a-client-secret",
        access_token="an-access-token",
        refresh_token="a-refresh-token",
    )


class TestTheHandshakeSucceeds:
    async def test_a_demo_account_authorises_against_a_practice_configuration(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(venue, settings())

        await connection.open()
        try:
            assert connection.is_authenticated
            assert connection.ctid_trader_account_id == DEMO_ACCOUNT.ctid_trader_account_id
        finally:
            await connection.close()

    async def test_it_authenticates_in_the_order_the_venue_requires(self) -> None:
        # Account authentication is refused by the venue unless the application has
        # authenticated first, and the account id it needs only exists once the account
        # list has been fetched, so this order is a protocol requirement.
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(venue, settings())

        await connection.open()
        await connection.close()

        assert venue.received_payload_types == [
            APPLICATION_AUTH_REQ,
            GET_ACCOUNTS_REQ,
            ACCOUNT_AUTH_REQ,
        ]

    async def test_a_live_account_authorises_against_a_live_configuration(self) -> None:
        venue = ScriptedVenue(accounts=[LIVE_ACCOUNT])
        connection = CTraderConnection(venue, settings(environment=VenueEnvironment.LIVE))

        await connection.open()
        try:
            assert connection.is_authenticated
        finally:
            await connection.close()


class TestTheLiveFlagAssertionFailsClosed:
    """The last check between a demo configuration and a real account.

    Every one of these must refuse. An assertion that only proves the accept path is
    not an assertion, it is a comment.
    """

    async def test_a_live_account_is_refused_by_a_practice_configuration(self) -> None:
        venue = ScriptedVenue(accounts=[LIVE_ACCOUNT])
        connection = CTraderConnection(venue, settings(environment=VenueEnvironment.PRACTICE))

        with pytest.raises(VenueAuthenticationError, match="is a live account"):
            await connection.open()
        assert not connection.is_authenticated

    async def test_a_demo_account_is_refused_by_a_live_configuration(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(venue, settings(environment=VenueEnvironment.LIVE))

        with pytest.raises(VenueAuthenticationError, match="is a demo account"):
            await connection.open()
        assert not connection.is_authenticated

    async def test_an_absent_flag_is_refused_rather_than_read_as_demo(self) -> None:
        # isLive is optional in the schema, so protobuf reports False for an account
        # that never carried the field. Reading that as "demo" would authorise an
        # unknown account against a demo configuration, which is exactly backwards.
        venue = ScriptedVenue(accounts=[FLAGLESS_ACCOUNT])
        connection = CTraderConnection(venue, settings(environment=VenueEnvironment.PRACTICE))

        with pytest.raises(VenueAuthenticationError, match="did not report isLive"):
            await connection.open()
        assert not connection.is_authenticated

    async def test_the_account_is_never_authorised_when_the_flag_is_wrong(self) -> None:
        # The refusal must happen before the account auth request is sent, not after.
        venue = ScriptedVenue(accounts=[LIVE_ACCOUNT])
        connection = CTraderConnection(venue, settings(environment=VenueEnvironment.PRACTICE))

        with pytest.raises(VenueAuthenticationError):
            await connection.open()

        assert ACCOUNT_AUTH_REQ not in venue.received_payload_types

    async def test_a_refused_handshake_closes_the_channel(self) -> None:
        # A half open connection that a caller forgot to close would keep a socket and
        # two tasks alive for a session that was refused.
        venue = ScriptedVenue(accounts=[LIVE_ACCOUNT])
        connection = CTraderConnection(venue, settings(environment=VenueEnvironment.PRACTICE))

        with pytest.raises(VenueAuthenticationError):
            await connection.open()

        assert venue.closed


class TestTheAccountMustBeResolvable:
    async def test_an_account_the_token_does_not_grant_is_refused(self) -> None:
        other = ScriptedAccount(ctid_trader_account_id=9, trader_login=1_111_111, is_live=False)
        venue = ScriptedVenue(accounts=[other])
        connection = CTraderConnection(venue, settings(account_id="5325402"))

        with pytest.raises(VenueAuthenticationError, match="is not among the accounts"):
            await connection.open()

    async def test_an_empty_account_list_is_refused(self) -> None:
        venue = ScriptedVenue(accounts=[])
        connection = CTraderConnection(venue, settings())

        with pytest.raises(VenueAuthenticationError, match="is not among the accounts"):
            await connection.open()

    async def test_a_non_numeric_account_id_is_refused(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(venue, settings(account_id="not-a-number"))

        with pytest.raises(VenueAuthenticationError, match="is not a number"):
            await connection.open()

    async def test_the_trader_login_is_matched_not_the_ctid(self) -> None:
        # The configured account number is the broker's login, and the id used for
        # every later request is a different number the venue assigns. Matching the
        # wrong one authorises the wrong account.
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(
            venue, settings(account_id=str(DEMO_ACCOUNT.ctid_trader_account_id))
        )

        with pytest.raises(VenueAuthenticationError, match="is not among the accounts"):
            await connection.open()


class TestCredentialFailures:
    async def test_a_rejected_application_credential_raises_authentication(self) -> None:
        venue = ScriptedVenue(
            accounts=[DEMO_ACCOUNT], application_auth_error="CH_CLIENT_AUTH_FAILURE"
        )
        connection = CTraderConnection(venue, settings())

        with pytest.raises(VenueAuthenticationError, match="CH_CLIENT_AUTH_FAILURE"):
            await connection.open()

    async def test_the_account_identifier_is_refused_before_the_handshake_completes(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(venue, settings())

        with pytest.raises(VenueAuthenticationError, match="not known until the handshake"):
            _ = connection.ctid_trader_account_id


class TestSilenceIsDeath:
    """A socket that stops answering must not look alive.

    This is the shape that produced the Bybit stream defect: an exception the catch
    clause did not name, and a recorder that would have exited instead of reconnecting.
    Here the equivalent is a connection that answers nothing and is never noticed.
    """

    async def test_a_venue_that_never_answers_kills_the_connection(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT], answer_application_auth=False)
        connection = CTraderConnection(
            venue, settings(stream_read_timeout_seconds=0.05, request_timeout_seconds=5.0)
        )

        with pytest.raises(VenueConnectivityError, match="not even a heartbeat"):
            await connection.open()
        assert not connection.is_authenticated

    async def test_a_request_on_a_dead_connection_is_refused_immediately(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT], answer_application_auth=False)
        connection = CTraderConnection(venue, settings(stream_read_timeout_seconds=0.05))

        with pytest.raises(VenueConnectivityError):
            await connection.open()

        assert connection.stats.last_error is not None
        assert "heartbeat" in connection.stats.last_error

    async def test_a_disconnect_event_kills_the_connection(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(venue, settings())
        await connection.open()
        try:
            venue.push_client_disconnect()
            await asyncio.wait_for(connection.wait_closed(), timeout=2.0)
            assert not connection.is_authenticated
        finally:
            await connection.close()


class TestTheHeartbeat:
    async def test_heartbeats_are_sent_on_the_interval(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(
            venue, settings(heartbeat_interval_seconds=0.02, stream_read_timeout_seconds=60.0)
        )
        await connection.open()
        try:
            await asyncio.sleep(0.15)
            assert venue.heartbeats_received >= 2
            assert connection.stats.heartbeats_sent >= 2
        finally:
            await connection.close()

    async def test_a_heartbeat_that_cannot_be_written_kills_the_connection(self) -> None:
        # The requirement is that this is death, not a logged warning. A connection
        # whose heartbeat fails is a connection that cannot carry an order, and every
        # caller must learn that from the connection rather than from the log.
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT], fail_write_after=3)
        connection = CTraderConnection(
            venue, settings(heartbeat_interval_seconds=0.02, stream_read_timeout_seconds=60.0)
        )
        await connection.open()
        try:
            await asyncio.wait_for(connection.wait_closed(), timeout=2.0)
            assert not connection.is_authenticated
            assert connection.stats.last_error is not None
            assert "ConnectionResetError" in connection.stats.last_error
        finally:
            await connection.close()

    async def test_a_received_heartbeat_is_counted_and_not_dispatched_as_an_event(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(venue, settings())
        await connection.open()
        try:
            before = connection.stats.events_received
            venue.push_heartbeat()
            await asyncio.sleep(0.05)
            assert connection.stats.heartbeats_received >= 1
            assert connection.stats.events_received == before
        finally:
            await connection.close()


class TestClosing:
    async def test_close_is_idempotent(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(venue, settings())
        await connection.open()

        await connection.close()
        await connection.close()

        assert venue.closed
        assert not connection.is_authenticated

    async def test_close_stops_the_background_tasks(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(
            venue, settings(heartbeat_interval_seconds=0.02, stream_read_timeout_seconds=60.0)
        )
        await connection.open()
        await connection.close()

        sent_at_close = connection.stats.heartbeats_sent
        await asyncio.sleep(0.1)
        assert connection.stats.heartbeats_sent == sent_at_close


class TestUnsolicitedEventHandler:
    """Registration after construction, which the constructor alone cannot express.

    A spot subscription is keyed by numeric symbol id, and those ids come from a
    catalogue fetch over the same connection, so the subscriber does not exist until
    the connection is open. The constructor argument stays for callers that do know
    their handler up front.
    """

    async def test_a_handler_registered_after_open_receives_events(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(venue, settings())
        received: list[int] = []

        async def handler(envelope: ProtoMessage) -> None:
            received.append(envelope.payloadType)

        await connection.open()
        try:
            connection.on_unsolicited(handler)
            event = ProtoOASpotEvent()
            event.ctidTraderAccountId = DEMO_ACCOUNT.ctid_trader_account_id
            event.symbolId = 1
            venue.push(envelope_for(SPOT_EVENT, event))
            await asyncio.sleep(0.05)
        finally:
            await connection.close()

        assert received == [SPOT_EVENT]

    async def test_replacing_a_handler_is_refused(self) -> None:
        """Silently replacing it would leave the previous subscriber alive, holding
        state it believes is being fed, reporting a quiet market rather than an error."""
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])

        async def first(_envelope: ProtoMessage) -> None:
            return None

        async def second(_envelope: ProtoMessage) -> None:
            return None

        connection = CTraderConnection(venue, settings(), on_event=first)
        with pytest.raises(TradingSysError, match="already set"):
            connection.on_unsolicited(second)

    async def test_registering_twice_is_refused(self) -> None:
        venue = ScriptedVenue(accounts=[DEMO_ACCOUNT])
        connection = CTraderConnection(venue, settings())

        async def handler(_envelope: ProtoMessage) -> None:
            return None

        connection.on_unsolicited(handler)
        with pytest.raises(TradingSysError, match="already set"):
            connection.on_unsolicited(handler)
