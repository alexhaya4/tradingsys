"""Length prefixed protobuf framing for the cTrader Open API socket.

Every message on the wire is a four byte big endian unsigned length followed by
exactly that many bytes of serialised :class:`ProtoMessage`. The envelope carries a
payload type discriminator, the serialised body, and an optional client message id
that a response echoes back, which is what lets a request be matched to its answer on
a connection where events arrive unsolicited.

The framing is separated from the connection so that it can be tested against bytes
rather than against a socket. Every decoding failure raises rather than returning a
partial or default message: a desynchronised stream must stop the connection, because
a frame boundary that is wrong once is wrong for everything after it.
"""

from __future__ import annotations

import struct
from typing import Final

from google.protobuf.message import DecodeError, Message

from tradingsys.venues.ctrader.messages.OpenApiCommonMessages_pb2 import ProtoMessage
from tradingsys.venues.errors import VenueResponseError

__all__ = [
    "LENGTH_PREFIX_BYTES",
    "MAX_FRAME_BYTES",
    "VENUE",
    "decode_envelope",
    "decode_length_prefix",
    "encode_frame",
    "envelope_for",
    "length_prefix",
    "parse_payload",
]

VENUE: Final = "ctrader"

LENGTH_PREFIX_BYTES: Final = 4
"""Width of the length prefix. Fixed by the venue's protocol, not a choice."""

_LENGTH_STRUCT: Final = struct.Struct(">I")

MAX_FRAME_BYTES: Final = 16 * 1024 * 1024
"""Largest frame this client will accept before declaring the stream desynchronised.

Not a venue limit and not a tuning value. A four byte prefix can announce four
gigabytes, so a stream that has lost its framing will ask this client to allocate an
arbitrary buffer and wait for bytes that never come. The bound is well above the
largest legitimate message, which is the full symbol list for an account, and far
below anything that threatens the process.
"""


def length_prefix(size: int) -> bytes:
    """Encode a frame length.

    Args:
        size: Number of bytes in the frame body.

    Raises:
        VenueResponseError: The size cannot be represented, or exceeds
            :data:`MAX_FRAME_BYTES`.
    """
    if size < 0 or size > MAX_FRAME_BYTES:
        raise VenueResponseError(
            VENUE, f"refusing to frame {size} bytes; the limit is {MAX_FRAME_BYTES}"
        )
    return _LENGTH_STRUCT.pack(size)


def decode_length_prefix(prefix: bytes) -> int:
    """Decode a frame length, rejecting anything beyond :data:`MAX_FRAME_BYTES`.

    Args:
        prefix: Exactly :data:`LENGTH_PREFIX_BYTES` bytes.

    Raises:
        VenueResponseError: The prefix is the wrong width, announces an empty frame,
            or announces more than this client will accept.
    """
    if len(prefix) != LENGTH_PREFIX_BYTES:
        raise VenueResponseError(
            VENUE,
            f"a frame length prefix must be {LENGTH_PREFIX_BYTES} bytes, got {len(prefix)}",
        )
    (size,) = _LENGTH_STRUCT.unpack(prefix)
    announced = int(size)
    if announced == 0:
        raise VenueResponseError(VENUE, "the venue announced a zero length frame")
    if announced > MAX_FRAME_BYTES:
        raise VenueResponseError(
            VENUE,
            f"the venue announced a {announced} byte frame, above the {MAX_FRAME_BYTES} "
            f"byte limit; the stream is desynchronised and cannot be trusted to resume",
        )
    return announced


def envelope_for(
    payload_type: int, message: Message, client_msg_id: str | None = None
) -> ProtoMessage:
    """Wrap a payload message in the transport envelope.

    Args:
        payload_type: The venue's discriminator for this message type. Supplied by the
            caller rather than read from the message, because the payload type field
            inside each message is optional in the schema and therefore not reliably
            populated by construction.
        message: The message to carry.
        client_msg_id: Correlation id echoed back on the response. Omitted for
            messages that expect no answer.
    """
    envelope = ProtoMessage()
    envelope.payloadType = payload_type
    envelope.payload = message.SerializeToString()
    if client_msg_id is not None:
        envelope.clientMsgId = client_msg_id
    return envelope


def encode_frame(envelope: ProtoMessage) -> bytes:
    """Serialise an envelope and prepend its length."""
    body = envelope.SerializeToString()
    return length_prefix(len(body)) + body


def decode_envelope(body: bytes) -> ProtoMessage:
    """Parse one frame body into an envelope.

    Raises:
        VenueResponseError: The bytes are not a parseable envelope, or the envelope
            omits the payload type that every message on this protocol must carry.
    """
    envelope = ProtoMessage()
    try:
        envelope.ParseFromString(body)
    except (DecodeError, ValueError) as exc:
        raise VenueResponseError(
            VENUE, f"could not parse a {len(body)} byte frame as an envelope: {exc}"
        ) from exc
    if not envelope.HasField("payloadType"):
        raise VenueResponseError(
            VENUE, "the venue sent an envelope with no payloadType, so it cannot be dispatched"
        )
    return envelope


def parse_payload(envelope: ProtoMessage, message: Message) -> Message:
    """Parse an envelope's payload into a message of the expected type.

    Args:
        envelope: The received envelope.
        message: An empty instance of the expected type, populated in place.

    Raises:
        VenueResponseError: The payload is absent or does not parse as that type.
    """
    if not envelope.HasField("payload"):
        raise VenueResponseError(
            VENUE,
            f"payload type {envelope.payloadType} arrived with no payload, "
            f"so {type(message).__name__} cannot be read from it",
        )
    try:
        message.ParseFromString(envelope.payload)
    except (DecodeError, ValueError) as exc:
        raise VenueResponseError(
            VENUE,
            f"payload type {envelope.payloadType} did not parse as {type(message).__name__}: {exc}",
        ) from exc
    return message
