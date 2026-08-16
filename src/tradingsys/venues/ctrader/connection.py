"""The cTrader Open API transport: a TLS socket carrying length prefixed protobuf.

Three things about this connection shape drive the design.

**It is bidirectional and asynchronous.** Responses are matched to requests by an
echoed client message id, and events arrive unsolicited on the same socket, so a
single reader task owns the stream and hands each frame either to a waiting caller or
to the event handler. Nothing else reads from the channel.

**Silence is indistinguishable from death.** A TCP connection that has lost its peer
looks open until something is written to it, and can stay that way indefinitely. The
venue sends heartbeats for exactly this reason, so this client treats a missing
heartbeat as a dead connection rather than as something to log. That is the same
failure the Bybit stream met when its reconnect clause omitted the library's own
disconnect exception, and it is not being rediscovered here.

**Demo and live are one code path apart.** The account list reports whether an account
is live, and that flag is asserted against the configured environment before the
account is authorised. It fails closed: absent, unreadable, or mismatched all refuse
the connection, because this is the last check between a demo configuration and a
real account.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, Self, final

from tradingsys.config.settings import VenueEnvironment
from tradingsys.observability.logging import get_logger
from tradingsys.venues.ctrader.framing import (
    LENGTH_PREFIX_BYTES,
    VENUE,
    decode_envelope,
    decode_length_prefix,
    encode_frame,
    envelope_for,
    parse_payload,
)
from tradingsys.venues.ctrader.messages.OpenApiCommonMessages_pb2 import (
    ProtoErrorRes,
    ProtoHeartbeatEvent,
    ProtoMessage,
)
from tradingsys.venues.ctrader.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
    ProtoOAApplicationAuthReq,
    ProtoOAApplicationAuthRes,
    ProtoOAErrorRes,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetAccountListByAccessTokenRes,
)
from tradingsys.venues.errors import (
    VenueAuthenticationError,
    VenueConnectivityError,
    VenueResponseError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from google.protobuf.message import Message

    from tradingsys.config.settings import ForexVenueSettings
    from tradingsys.venues.ctrader.messages.OpenApiModelMessages_pb2 import (
        ProtoOACtidTraderAccount,
    )

__all__ = [
    "ByteChannel",
    "CTraderConnection",
    "ConnectionStats",
    "TlsChannel",
]

logger = get_logger("venues.ctrader.connection")

VENUE_HEARTBEAT_SECONDS: Final = 30.0
"""How often the venue sends its own heartbeat, measured rather than assumed.

Observed against demo.ctraderapi.com on 2026-08-16 over a 150 second idle window:
arrivals at t+30.1, 60.1, 90.2, 120.2, 150.1, so gaps of 30.0, 30.1, 30.0, 29.9.

This is the number the read deadline has to be sized against. An idle forex connection
carries nothing but these, which is the normal state of the socket over a weekend, so a
deadline shorter than this interval declares a perfectly healthy connection dead. The
first draft of this client shipped a 20 second deadline and would have done exactly
that; it was caught by connecting to the venue rather than by any test.
"""

HEARTBEAT_PAYLOAD_TYPE: Final = 51
ERROR_RES_PAYLOAD_TYPE: Final = 50
APPLICATION_AUTH_REQ: Final = 2100
APPLICATION_AUTH_RES: Final = 2101
ACCOUNT_AUTH_REQ: Final = 2102
ACCOUNT_AUTH_RES: Final = 2103
OA_ERROR_RES: Final = 2142
CLIENT_DISCONNECT_EVENT: Final = 2148
GET_ACCOUNTS_REQ: Final = 2149
GET_ACCOUNTS_RES: Final = 2150
ACCOUNT_DISCONNECT_EVENT: Final = 2164


class ByteChannel(Protocol):
    """A bidirectional byte stream.

    Narrower than an asyncio stream pair on purpose: the connection needs exactly
    these operations, and a test can supply a scripted implementation without a
    socket, a certificate, or a listening server.
    """

    async def read_exactly(self, count: int) -> bytes:
        """Read exactly ``count`` bytes, or raise if the stream ends first."""
        ...

    async def write(self, data: bytes) -> None:
        """Write all of ``data``."""
        ...

    async def close(self) -> None:
        """Close the stream. Safe to call more than once."""
        ...


@final
class TlsChannel:
    """A :class:`ByteChannel` over a TLS socket.

    TLS is mandatory on this venue's protobuf port and there is no plaintext fallback,
    so certificate verification is left at the default rather than being made
    configurable. A venue connection that can be talked out of verifying its peer is
    worth less than no connection at all.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(cls, host: str, port: int, *, timeout_seconds: float) -> Self:
        """Open a verified TLS connection.

        Raises:
            VenueConnectivityError: The endpoint could not be reached, the handshake
                failed, or the certificate did not verify.
        """
        context = ssl.create_default_context()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=context, server_hostname=host),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise VenueConnectivityError(
                VENUE, f"connecting to {host}:{port} timed out after {timeout_seconds}s"
            ) from exc
        except ssl.SSLError as exc:
            raise VenueConnectivityError(
                VENUE, f"the TLS handshake with {host}:{port} failed: {exc}"
            ) from exc
        except OSError as exc:
            raise VenueConnectivityError(VENUE, f"could not reach {host}:{port}: {exc}") from exc
        return cls(reader, writer)

    async def read_exactly(self, count: int) -> bytes:
        try:
            return await self._reader.readexactly(count)
        except asyncio.IncompleteReadError as exc:
            raise VenueConnectivityError(
                VENUE,
                f"the connection ended after {len(exc.partial)} of {count} expected bytes",
            ) from exc
        except OSError as exc:
            raise VenueConnectivityError(
                VENUE, f"reading from the connection failed: {exc}"
            ) from exc

    async def write(self, data: bytes) -> None:
        try:
            self._writer.write(data)
            await self._writer.drain()
        except OSError as exc:
            raise VenueConnectivityError(VENUE, f"writing to the connection failed: {exc}") from exc

    async def close(self) -> None:
        self._writer.close()
        # A TLS close can raise if the peer has already gone, which is not a failure of
        # closing: the socket ends up closed either way and the caller is shutting down.
        with contextlib.suppress(OSError, ssl.SSLError, asyncio.CancelledError):
            await self._writer.wait_closed()


@dataclass(slots=True)
class ConnectionStats:
    """What this connection has done, for health reporting and for tests.

    Counters rather than log lines, because "did the feed stall" is a question about a
    rate, and a log line cannot be queried for one.
    """

    frames_sent: int = 0
    frames_received: int = 0
    heartbeats_sent: int = 0
    heartbeats_received: int = 0
    events_received: int = 0
    last_error: str | None = None


@final
class CTraderConnection:
    """An authenticated cTrader Open API session over one channel.

    Construct with a channel and open it with :meth:`open`, which performs the full
    handshake: application authentication, account discovery, the live flag assertion,
    then account authentication. The connection is unusable until that completes and
    refuses to pretend otherwise.
    """

    def __init__(
        self,
        channel: ByteChannel,
        settings: ForexVenueSettings,
        *,
        on_event: Callable[[ProtoMessage], Coroutine[object, object, None]] | None = None,
    ) -> None:
        self._channel = channel
        self._settings = settings
        self._on_event = on_event
        self._pending: dict[str, asyncio.Future[ProtoMessage]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._death: BaseException | None = None
        self._authenticated = False
        self._ctid_trader_account_id: int | None = None
        self.stats = ConnectionStats()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated and self._death is None

    @property
    def ctid_trader_account_id(self) -> int:
        """The venue's own account identifier, resolved during the handshake.

        Raises:
            VenueAuthenticationError: The handshake has not completed, so there is no
                identifier to give and returning a placeholder would be a lie.
        """
        if self._ctid_trader_account_id is None:
            raise VenueAuthenticationError(
                VENUE, "the account identifier is not known until the handshake has completed"
            )
        return self._ctid_trader_account_id

    async def open(self) -> None:
        """Start the reader and heartbeat, then authenticate.

        Raises:
            VenueAuthenticationError: Credentials were refused, the configured account
                is not among those the token grants, or its live flag does not match
                the configured environment.
            VenueConnectivityError: The connection died during the handshake.
        """
        self._reader_task = asyncio.create_task(self._read_forever(), name="ctrader-reader")
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_forever(), name="ctrader-heartbeat"
        )
        try:
            await self._authenticate()
        except BaseException:
            # A half authenticated connection must not survive. Closing here means a
            # caller that catches the error does not also have to remember to clean up.
            await self.close()
            raise

    async def close(self) -> None:
        """Stop the tasks and close the channel. Safe to call more than once."""
        self._authenticated = False
        for task in (self._reader_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
        for task in (self._reader_task, self._heartbeat_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = None
        self._heartbeat_task = None
        self._fail_pending(
            self._death
            if self._death is not None
            else VenueConnectivityError(VENUE, "the connection was closed")
        )
        await self._channel.close()
        self._closed.set()

    async def wait_closed(self) -> None:
        """Block until the connection is closed or has died."""
        await self._closed.wait()

    # ------------------------------------------------------------------
    # request and response
    # ------------------------------------------------------------------

    async def request(
        self, payload_type: int, message: Message, response: Message, expected_payload_type: int
    ) -> Message:
        """Send a request and wait for the matching response.

        Args:
            payload_type: Discriminator for the request.
            message: The request body.
            response: An empty instance of the expected response, populated in place.
            expected_payload_type: Discriminator the response must carry. Checked
                rather than assumed, so a venue that answers with a different message
                is a loud failure instead of a silently empty result.

        Raises:
            VenueConnectivityError: The connection died before the answer arrived, or
                the venue did not answer within the configured request timeout.
            VenueResponseError: The answer carried the wrong payload type or did not
                parse.
            VenueAuthenticationError: The venue answered with an error naming a
                credential problem.
        """
        self._raise_if_dead()
        client_msg_id = str(uuid.uuid4())
        waiter: asyncio.Future[ProtoMessage] = asyncio.get_running_loop().create_future()
        self._pending[client_msg_id] = waiter
        try:
            await self._send(envelope_for(payload_type, message, client_msg_id))
            envelope = await asyncio.wait_for(
                waiter, timeout=self._settings.request_timeout_seconds
            )
        except TimeoutError as exc:
            raise VenueConnectivityError(
                VENUE,
                f"payload type {payload_type} was not answered within "
                f"{self._settings.request_timeout_seconds}s",
            ) from exc
        finally:
            self._pending.pop(client_msg_id, None)

        if envelope.payloadType != expected_payload_type:
            raise VenueResponseError(
                VENUE,
                f"expected payload type {expected_payload_type} in reply to {payload_type}, "
                f"got {envelope.payloadType}",
            )
        return parse_payload(envelope, response)

    async def _send(self, envelope: ProtoMessage) -> None:
        try:
            await self._channel.write(encode_frame(envelope))
        except VenueConnectivityError as exc:
            self._die(exc)
            raise
        self.stats.frames_sent += 1

    # ------------------------------------------------------------------
    # the reader
    # ------------------------------------------------------------------

    async def _read_forever(self) -> None:
        """Read frames until the connection dies or the deadline passes.

        Every exception other than cancellation kills the connection. There is no
        error here that leaves the stream usable: a framing failure has lost the
        boundaries, and a transport failure has lost the socket.
        """
        try:
            while True:
                envelope = await asyncio.wait_for(
                    self._read_one(), timeout=self._settings.stream_read_timeout_seconds
                )
                self._dispatch(envelope)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            self._die(
                VenueConnectivityError(
                    VENUE,
                    f"nothing arrived for {self._settings.stream_read_timeout_seconds}s, "
                    f"not even a heartbeat, so the connection is treated as dead",
                )
            )
            del exc
        except Exception as exc:
            self._die(exc)

    async def _read_one(self) -> ProtoMessage:
        prefix = await self._channel.read_exactly(LENGTH_PREFIX_BYTES)
        size = decode_length_prefix(prefix)
        body = await self._channel.read_exactly(size)
        self.stats.frames_received += 1
        return decode_envelope(body)

    def _dispatch(self, envelope: ProtoMessage) -> None:
        if envelope.payloadType == HEARTBEAT_PAYLOAD_TYPE:
            self.stats.heartbeats_received += 1
            return

        if envelope.HasField("clientMsgId"):
            waiter = self._pending.pop(envelope.clientMsgId, None)
            if waiter is not None and not waiter.done():
                failure = self._error_in(envelope)
                if failure is not None:
                    waiter.set_exception(failure)
                else:
                    waiter.set_result(envelope)
                return

        self.stats.events_received += 1
        if envelope.payloadType in (CLIENT_DISCONNECT_EVENT, ACCOUNT_DISCONNECT_EVENT):
            self._die(
                VenueConnectivityError(
                    VENUE,
                    f"the venue sent disconnect event {envelope.payloadType}; the session is over",
                )
            )
            return
        if self._on_event is not None:
            handler = self._on_event
            task: asyncio.Task[None] = asyncio.create_task(handler(envelope), name="ctrader-event")
            task.add_done_callback(self._report_event_failure)

    def _report_event_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("a ctrader event handler failed", exc_info=exc)

    def _error_in(self, envelope: ProtoMessage) -> Exception | None:
        """Turn an error response into the exception it deserves, or None."""
        if envelope.payloadType == OA_ERROR_RES:
            error = ProtoOAErrorRes()
            parse_payload(envelope, error)
            return self._classify(error.errorCode, error.description)
        if envelope.payloadType == ERROR_RES_PAYLOAD_TYPE:
            common = ProtoErrorRes()
            parse_payload(envelope, common)
            return self._classify(common.errorCode, common.description)
        return None

    @staticmethod
    def _classify(code: str, description: str) -> Exception:
        message = f"{code}: {description}" if description else code
        if "AUTH" in code.upper() or "TOKEN" in code.upper() or "PERMISSION" in code.upper():
            return VenueAuthenticationError(VENUE, message)
        return VenueResponseError(VENUE, message)

    # ------------------------------------------------------------------
    # the heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_forever(self) -> None:
        """Send heartbeats on an interval, and treat a failure to send as death.

        A heartbeat that cannot be written means the socket is gone. Logging that and
        continuing would leave a connection that looks alive to every caller while
        being incapable of carrying a single order.
        """
        interval = self._settings.heartbeat_interval_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                await self._send(envelope_for(HEARTBEAT_PAYLOAD_TYPE, ProtoHeartbeatEvent()))
                self.stats.heartbeats_sent += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._die(exc)

    # ------------------------------------------------------------------
    # death
    # ------------------------------------------------------------------

    def _die(self, cause: BaseException) -> None:
        """Record the connection as dead and fail everything waiting on it."""
        if self._death is not None:
            return
        self._death = cause
        self._authenticated = False
        self.stats.last_error = f"{type(cause).__name__}: {cause}"
        logger.warning("the ctrader connection died", reason=str(cause), venue=VENUE)
        self._fail_pending(cause)
        self._closed.set()

    def _fail_pending(self, cause: BaseException) -> None:
        for waiter in list(self._pending.values()):
            if not waiter.done():
                waiter.set_exception(cause)
        self._pending.clear()

    def _raise_if_dead(self) -> None:
        if self._death is not None:
            raise VenueConnectivityError(VENUE, f"the connection is dead: {self._death}")

    # ------------------------------------------------------------------
    # the handshake
    # ------------------------------------------------------------------

    async def _authenticate(self) -> None:
        client_id = _required_secret(self._settings.client_id, "client_id")
        client_secret = _required_secret(self._settings.client_secret, "client_secret")
        access_token = _required_secret(self._settings.access_token, "access_token")

        application = ProtoOAApplicationAuthReq()
        application.clientId = client_id
        application.clientSecret = client_secret
        await self.request(
            APPLICATION_AUTH_REQ,
            application,
            ProtoOAApplicationAuthRes(),
            APPLICATION_AUTH_RES,
        )
        logger.info("ctrader application authenticated", venue=VENUE)

        account = await self._resolve_account(access_token)
        self._assert_environment_matches(account)

        account_auth = ProtoOAAccountAuthReq()
        account_auth.ctidTraderAccountId = int(account.ctidTraderAccountId)
        account_auth.accessToken = access_token
        confirmed = await self.request(
            ACCOUNT_AUTH_REQ, account_auth, ProtoOAAccountAuthRes(), ACCOUNT_AUTH_RES
        )
        if not isinstance(confirmed, ProtoOAAccountAuthRes):  # pragma: no cover - defensive
            raise VenueResponseError(VENUE, "account authentication returned the wrong type")
        if confirmed.ctidTraderAccountId != account.ctidTraderAccountId:
            raise VenueAuthenticationError(
                VENUE,
                f"authorised account {confirmed.ctidTraderAccountId} is not the account "
                f"{account.ctidTraderAccountId} that was requested",
            )

        self._ctid_trader_account_id = int(account.ctidTraderAccountId)
        self._authenticated = True
        logger.info(
            "ctrader account authenticated",
            venue=VENUE,
            ctid_trader_account_id=self._ctid_trader_account_id,
            environment=self._settings.environment.value,
        )

    async def _resolve_account(self, access_token: str) -> ProtoOACtidTraderAccount:
        """Find the configured account among those the access token grants.

        The configured ``account_id`` is the broker's account number, which the venue
        reports as ``traderLogin``. The identifier used for every subsequent request is
        ``ctidTraderAccountId``, which is a different number, so the two cannot be used
        interchangeably and the mapping has to come from the venue.
        """
        configured = self._settings.account_id
        if configured is None:
            raise VenueAuthenticationError(
                VENUE, "no account_id is configured, so no account can be authorised"
            )

        listing = ProtoOAGetAccountListByAccessTokenReq()
        listing.accessToken = access_token
        result = await self.request(
            GET_ACCOUNTS_REQ,
            listing,
            ProtoOAGetAccountListByAccessTokenRes(),
            GET_ACCOUNTS_RES,
        )
        if not isinstance(result, ProtoOAGetAccountListByAccessTokenRes):  # pragma: no cover
            raise VenueResponseError(VENUE, "the account list returned the wrong type")

        try:
            wanted = int(configured)
        except ValueError as exc:
            raise VenueAuthenticationError(
                VENUE, f"account_id {configured!r} is not a number, so it cannot match an account"
            ) from exc

        for candidate in result.ctidTraderAccount:
            if candidate.HasField("traderLogin") and int(candidate.traderLogin) == wanted:
                return candidate

        available = ", ".join(
            str(candidate.traderLogin) if candidate.HasField("traderLogin") else "unknown"
            for candidate in result.ctidTraderAccount
        )
        raise VenueAuthenticationError(
            VENUE,
            f"account {wanted} is not among the accounts this access token grants "
            f"({available or 'none'}); the token belongs to a different cTID",
        )

    def _assert_environment_matches(self, account: ProtoOACtidTraderAccount) -> None:
        """Refuse unless the account's live flag matches the configured environment.

        This fails closed. An absent flag refuses, because ``isLive`` is optional in
        the schema and a missing optional bool reads as false, which would silently
        treat an unreadable account as a demo one. That is the single most expensive
        default this system could take, so it is not taken.
        """
        if not account.HasField("isLive"):
            raise VenueAuthenticationError(
                VENUE,
                f"account {account.ctidTraderAccountId} did not report isLive. The flag is "
                f"optional in the schema, so its absence reads as false and would treat an "
                f"unknown account as a demo one. Refusing the connection instead.",
            )

        expects_live = self._settings.environment is VenueEnvironment.LIVE
        if bool(account.isLive) != expects_live:
            configured = "live" if expects_live else "demo"
            reported = "live" if account.isLive else "demo"
            raise VenueAuthenticationError(
                VENUE,
                f"account {account.ctidTraderAccountId} is a {reported} account but this "
                f"process is configured for {configured}. Refusing to authorise it.",
            )
        logger.info(
            "ctrader account environment confirmed",
            venue=VENUE,
            is_live=bool(account.isLive),
            configured=self._settings.environment.value,
        )


def _required_secret(value: object, name: str) -> str:
    """Read a secret that the handshake cannot proceed without.

    Raises:
        VenueAuthenticationError: The value is absent or empty. Configuration
            validation already refuses an enabled venue without credentials; this is
            the second gate, because the handshake must never send an empty secret and
            interpret the venue's refusal as a credential problem.
    """
    if value is None:
        raise VenueAuthenticationError(VENUE, f"{name} is not configured")
    secret = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    if not secret:
        raise VenueAuthenticationError(VENUE, f"{name} is configured but empty")
    return str(secret)
