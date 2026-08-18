"""A hand written cTrader peer, for driving the transport without a socket.

There are no mocks in this suite. This is a real implementation of
:class:`~tradingsys.venues.ctrader.connection.ByteChannel` that speaks the venue's
protocol back at the client: it decodes each frame the client writes, decides what the
venue would answer, and puts the reply where the client's reader will find it.

Written by hand rather than generated from a recording because the cases that matter
here are the ones a recording cannot contain: a venue that answers nothing at all, a
socket that fails on write, an account whose ``isLive`` flag is absent. Those are the
paths the transport exists to survive, so they are the paths that need a peer that can
be told to produce them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from tradingsys.venues.ctrader.framing import (
    LENGTH_PREFIX_BYTES,
    decode_envelope,
    decode_length_prefix,
    encode_frame,
    envelope_for,
    parse_payload,
)
from tradingsys.venues.ctrader.messages.OpenApiCommonMessages_pb2 import (
    ProtoHeartbeatEvent,
    ProtoMessage,
)
from tradingsys.venues.ctrader.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
    ProtoOAApplicationAuthRes,
    ProtoOAAssetListRes,
    ProtoOAErrorRes,
    ProtoOAGetAccountListByAccessTokenRes,
    ProtoOASymbolByIdReq,
    ProtoOASymbolByIdRes,
    ProtoOASymbolsListRes,
)
from tradingsys.venues.ctrader.messages.OpenApiModelMessages_pb2 import (
    ProtoOALightSymbol,
    ProtoOASymbol,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

APPLICATION_AUTH_REQ: Final = 2100
APPLICATION_AUTH_RES: Final = 2101
ACCOUNT_AUTH_REQ: Final = 2102
ACCOUNT_AUTH_RES: Final = 2103
OA_ERROR_RES: Final = 2142
CLIENT_DISCONNECT_EVENT: Final = 2148
GET_ACCOUNTS_REQ: Final = 2149
GET_ACCOUNTS_RES: Final = 2150
HEARTBEAT_PAYLOAD_TYPE: Final = 51
ASSET_LIST_REQ: Final = 2112
ASSET_LIST_RES: Final = 2113
SYMBOLS_LIST_REQ: Final = 2114
SYMBOLS_LIST_RES: Final = 2115
SYMBOL_BY_ID_REQ: Final = 2116
SYMBOL_BY_ID_RES: Final = 2117


@dataclass(frozen=True, slots=True)
class ScriptedAccount:
    """One account the venue will report.

    ``is_live`` is deliberately three valued. ``None`` means the venue omits the field
    entirely, which is legal in the schema and is the case the client must refuse
    rather than read as false.
    """

    ctid_trader_account_id: int
    trader_login: int
    is_live: bool | None


@dataclass(slots=True)
class ScriptedVenue:
    """A cTrader peer that answers the handshake.

    Attributes:
        accounts: What the account list request returns.
        assets: Asset id to name, as the venue publishes them.
        light_symbols: The lightweight symbol records the catalogue lists.
        full_symbols: Symbol id to full definition, for the by-id lookup. A symbol
            absent here is absent from the venue's reply, which is the case the client
            must refuse rather than read as an empty definition.
        application_auth_error: Error code to answer application auth with, if any.
        answer_application_auth: When false, the venue stays silent, which is how a
            read deadline is exercised.
        fail_write_after: Number of successful writes before every further write
            raises, which is how a dead socket is exercised.
    """

    accounts: Sequence[ScriptedAccount] = ()
    application_auth_error: str | None = None
    answer_application_auth: bool = True
    fail_write_after: int | None = None

    # The catalogue. Empty by default so that transport tests are unaffected: a venue
    # that lists nothing is a venue the handshake tests never ask about symbols.
    assets: Mapping[int, str] = field(default_factory=dict)
    light_symbols: Sequence[ProtoOALightSymbol] = ()
    full_symbols: Mapping[int, ProtoOASymbol] = field(default_factory=dict)

    received_payload_types: list[int] = field(default_factory=list)
    heartbeats_received: int = 0
    closed: bool = False
    _chunks: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue, init=False)
    _buffer: bytearray = field(default_factory=bytearray, init=False)
    _writes: int = field(default=0, init=False)

    # ------------------------------------------------------------------
    # the ByteChannel side
    # ------------------------------------------------------------------

    async def read_exactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            self._buffer.extend(await self._chunks.get())
        taken = bytes(self._buffer[:count])
        del self._buffer[:count]
        return taken

    async def write(self, data: bytes) -> None:
        self._writes += 1
        if self.fail_write_after is not None and self._writes > self.fail_write_after:
            raise ConnectionResetError("the scripted socket is gone")
        self._respond_to(data)

    async def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------
    # the venue side
    # ------------------------------------------------------------------

    def push(self, envelope: ProtoMessage) -> None:
        """Deliver an unsolicited frame to the client."""
        self._chunks.put_nowait(encode_frame(envelope))

    def _respond_to(self, frame: bytes) -> None:
        envelope = decode_envelope(frame[LENGTH_PREFIX_BYTES:])
        assert decode_length_prefix(frame[:LENGTH_PREFIX_BYTES]) == len(frame) - LENGTH_PREFIX_BYTES
        self.received_payload_types.append(envelope.payloadType)
        client_msg_id = envelope.clientMsgId if envelope.HasField("clientMsgId") else None

        if envelope.payloadType == HEARTBEAT_PAYLOAD_TYPE:
            self.heartbeats_received += 1
            return

        if self._respond_to_handshake(envelope, client_msg_id):
            return
        if self._respond_to_catalogue(envelope.payloadType, envelope, client_msg_id):
            return
        self._push_error("UNSUPPORTED_MESSAGE", client_msg_id)

    def _respond_to_handshake(self, envelope: ProtoMessage, client_msg_id: str | None) -> bool:
        """Answer the three handshake requests. Returns whether it handled the message."""
        if envelope.payloadType == APPLICATION_AUTH_REQ:
            if not self.answer_application_auth:
                return True
            if self.application_auth_error is not None:
                self._push_error(self.application_auth_error, client_msg_id)
                return True
            self.push(
                envelope_for(APPLICATION_AUTH_RES, ProtoOAApplicationAuthRes(), client_msg_id)
            )
            return True

        if envelope.payloadType == GET_ACCOUNTS_REQ:
            listing = ProtoOAGetAccountListByAccessTokenRes()
            listing.accessToken = "scripted"
            for account in self.accounts:
                entry = listing.ctidTraderAccount.add()
                entry.ctidTraderAccountId = account.ctid_trader_account_id
                entry.traderLogin = account.trader_login
                if account.is_live is not None:
                    entry.isLive = account.is_live
            self.push(envelope_for(GET_ACCOUNTS_RES, listing, client_msg_id))
            return True

        if envelope.payloadType == ACCOUNT_AUTH_REQ:
            request = ProtoOAAccountAuthReq()
            parse_payload(envelope, request)
            confirmation = ProtoOAAccountAuthRes()
            confirmation.ctidTraderAccountId = request.ctidTraderAccountId
            self.push(envelope_for(ACCOUNT_AUTH_RES, confirmation, client_msg_id))
            return True

        return False

    def _respond_to_catalogue(
        self, payload_type: int, envelope: ProtoMessage, client_msg_id: str | None
    ) -> bool:
        """Answer the three catalogue requests. Returns whether it handled the message.

        Split out of :meth:`_respond_to` because the handshake and the catalogue are
        separate conversations with the venue, and keeping them in one method made the
        branch count say so.
        """
        if payload_type == ASSET_LIST_REQ:
            asset_listing = ProtoOAAssetListRes()
            asset_listing.ctidTraderAccountId = self.accounts[0].ctid_trader_account_id
            for asset_id, name in self.assets.items():
                asset = asset_listing.asset.add()
                asset.assetId = asset_id
                asset.name = name
            self.push(envelope_for(ASSET_LIST_RES, asset_listing, client_msg_id))
            return True

        if payload_type == SYMBOLS_LIST_REQ:
            symbols = ProtoOASymbolsListRes()
            symbols.ctidTraderAccountId = self.accounts[0].ctid_trader_account_id
            symbols.symbol.extend(self.light_symbols)
            self.push(envelope_for(SYMBOLS_LIST_RES, symbols, client_msg_id))
            return True

        if payload_type == SYMBOL_BY_ID_REQ:
            by_id = ProtoOASymbolByIdReq()
            parse_payload(envelope, by_id)
            answer = ProtoOASymbolByIdRes()
            answer.ctidTraderAccountId = self.accounts[0].ctid_trader_account_id
            for wanted in by_id.symbolId:
                # The venue answers with what it has. A symbol it does not know is
                # simply absent from the reply, which is the case the client has to
                # refuse rather than read as an empty definition.
                full = self.full_symbols.get(wanted)
                if full is not None:
                    answer.symbol.append(full)
            self.push(envelope_for(SYMBOL_BY_ID_RES, answer, client_msg_id))
            return True

        return False

    def _push_error(self, code: str, client_msg_id: str | None) -> None:
        error = ProtoOAErrorRes()
        error.errorCode = code
        error.description = f"scripted {code}"
        self.push(envelope_for(OA_ERROR_RES, error, client_msg_id))

    def push_heartbeat(self) -> None:
        self.push(envelope_for(HEARTBEAT_PAYLOAD_TYPE, ProtoHeartbeatEvent()))

    def push_client_disconnect(self) -> None:
        error = ProtoOAErrorRes()
        error.errorCode = "CLIENT_DISCONNECTED"
        self.push(envelope_for(CLIENT_DISCONNECT_EVENT, error))
