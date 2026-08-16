"""Framing: the four byte length prefix and the protobuf envelope.

Framing failures are tested against bytes rather than against a socket, because the
cases that matter are the malformed ones and a cooperating venue will not produce
them. A frame boundary that is wrong once is wrong for everything after it, so every
one of these must raise rather than return a partial or defaulted message.
"""

from __future__ import annotations

import pytest

from tradingsys.venues.ctrader.framing import (
    LENGTH_PREFIX_BYTES,
    MAX_FRAME_BYTES,
    decode_envelope,
    decode_length_prefix,
    encode_frame,
    envelope_for,
    length_prefix,
    parse_payload,
)
from tradingsys.venues.ctrader.messages.OpenApiCommonMessages_pb2 import (
    ProtoHeartbeatEvent,
    ProtoMessage,
)
from tradingsys.venues.ctrader.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAApplicationAuthReq,
)
from tradingsys.venues.errors import VenueResponseError

HEARTBEAT_PAYLOAD_TYPE = 51
APPLICATION_AUTH_REQ = 2100


class TestRoundTrip:
    def test_a_framed_envelope_decodes_to_what_was_encoded(self) -> None:
        request = ProtoOAApplicationAuthReq()
        request.clientId = "client"
        request.clientSecret = "secret"
        frame = encode_frame(envelope_for(APPLICATION_AUTH_REQ, request, "correlation-1"))

        announced = decode_length_prefix(frame[:LENGTH_PREFIX_BYTES])
        assert announced == len(frame) - LENGTH_PREFIX_BYTES

        envelope = decode_envelope(frame[LENGTH_PREFIX_BYTES:])
        assert envelope.payloadType == APPLICATION_AUTH_REQ
        assert envelope.clientMsgId == "correlation-1"

        recovered = ProtoOAApplicationAuthReq()
        parse_payload(envelope, recovered)
        assert recovered.clientId == "client"
        assert recovered.clientSecret == "secret"

    def test_the_prefix_is_big_endian(self) -> None:
        # Getting the byte order wrong produces a plausible looking length that is off
        # by orders of magnitude, which desynchronises the stream rather than failing.
        assert length_prefix(1) == b"\x00\x00\x00\x01"
        assert length_prefix(258) == b"\x00\x00\x01\x02"

    def test_a_message_without_a_correlation_id_carries_none(self) -> None:
        # Heartbeats expect no answer, so they carry no correlation id and the reader
        # must not go looking for a waiter that was never registered.
        envelope = envelope_for(HEARTBEAT_PAYLOAD_TYPE, ProtoHeartbeatEvent())
        assert not envelope.HasField("clientMsgId")
        assert envelope.payloadType == HEARTBEAT_PAYLOAD_TYPE


class TestMalformedFramesRaise:
    def test_a_prefix_of_the_wrong_width_is_refused(self) -> None:
        with pytest.raises(VenueResponseError, match="must be 4 bytes"):
            decode_length_prefix(b"\x00\x00")

    def test_a_zero_length_frame_is_refused(self) -> None:
        with pytest.raises(VenueResponseError, match="zero length frame"):
            decode_length_prefix(b"\x00\x00\x00\x00")

    def test_a_frame_larger_than_the_limit_is_refused(self) -> None:
        # A four byte prefix can announce four gigabytes. Allocating on a desynchronised
        # stream's say so is how a client is made to exhaust memory waiting for bytes
        # that will never arrive.
        oversized = (MAX_FRAME_BYTES + 1).to_bytes(LENGTH_PREFIX_BYTES, "big")
        with pytest.raises(VenueResponseError, match="desynchronised"):
            decode_length_prefix(oversized)

    def test_the_largest_permitted_frame_is_accepted(self) -> None:
        at_limit = MAX_FRAME_BYTES.to_bytes(LENGTH_PREFIX_BYTES, "big")
        assert decode_length_prefix(at_limit) == MAX_FRAME_BYTES

    def test_refusing_to_frame_an_oversized_body(self) -> None:
        with pytest.raises(VenueResponseError, match="refusing to frame"):
            length_prefix(MAX_FRAME_BYTES + 1)

    def test_bytes_that_are_not_an_envelope_are_refused(self) -> None:
        with pytest.raises(VenueResponseError, match="could not parse"):
            decode_envelope(b"\xff\xff\xff\xff\xff\xff")

    def test_an_envelope_without_a_payload_type_is_refused(self) -> None:
        # payloadType is required by the schema, so an envelope missing it cannot be
        # dispatched to anything and must not be guessed at.
        with pytest.raises(VenueResponseError, match="no payloadType"):
            decode_envelope(b"")


class TestPayloadParsing:
    def test_a_missing_payload_is_refused(self) -> None:
        envelope = ProtoMessage()
        envelope.payloadType = APPLICATION_AUTH_REQ
        with pytest.raises(VenueResponseError, match="no payload"):
            parse_payload(envelope, ProtoOAApplicationAuthReq())

    def test_a_payload_that_is_not_the_expected_type_is_refused(self) -> None:
        # Protobuf will happily parse arbitrary bytes into the wrong message when the
        # field numbers happen to line up, so this asserts on a body that cannot.
        envelope = ProtoMessage()
        envelope.payloadType = APPLICATION_AUTH_REQ
        envelope.payload = b"\x0a\xff"
        with pytest.raises(VenueResponseError, match="did not parse"):
            parse_payload(envelope, ProtoOAAccountAuthReq())

    def test_the_parsed_message_is_the_instance_that_was_passed_in(self) -> None:
        request = ProtoOAApplicationAuthReq()
        request.clientId = "id"
        request.clientSecret = "secret"
        envelope = envelope_for(APPLICATION_AUTH_REQ, request)

        target = ProtoOAApplicationAuthReq()
        assert parse_payload(envelope, target) is target
        assert target.clientId == "id"
