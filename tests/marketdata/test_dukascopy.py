"""Tests for the Dukascopy .bi5 reader.

Two things are being pinned here. The first is the wire format, byte for byte, using
fixtures built by the same struct layout the decoder claims the feed uses. The second,
and the reason the reader exists at all, is what happens to a payload that is not a
.bi5 file: the feed answers a burst of requests with an HTML error page, and anything
that quietly turns that into "no ticks this hour" writes a hole into the archive that
looks exactly like a quiet market and can never be told apart from one afterwards.

Most fixtures are built here rather than checked in, so the expected bytes are visible
in the test that depends on them. That only ever proves the decoder agrees with this
file's encoder, so one real hour recorded from the feed is checked in as well and
decoded in the last class, where the claim about Dukascopy is actually tested.
"""

from __future__ import annotations

import lzma
import struct
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tradingsys.core.errors import DomainError
from tradingsys.marketdata.dukascopy import (
    FEED_BASE_URL,
    RECORD_SIZE,
    Bi5DecodeError,
    DukascopyTick,
    decode_hour,
    decompress_bi5,
    hour_url,
)

HOUR = datetime(2025, 3, 5, 10, tzinfo=UTC)
HOUR_LENGTH = timedelta(hours=1)
RECORDED_HOUR = Path(__file__).parent / "data" / "EURUSD_2025-03-05_10h.bi5"
RECORDED_HOUR_START = HOUR
EURUSD_DIGITS = 5
JPY_DIGITS = 3


def record(offset_ms: int, ask: int, bid: int, ask_volume: float, bid_volume: float) -> bytes:
    """One 20 byte record, big endian, in the feed's field order: ask before bid."""
    return struct.pack(">IIIff", offset_ms, ask, bid, ask_volume, bid_volume)


def bi5(*records: bytes) -> bytes:
    return lzma.compress(b"".join(records), format=lzma.FORMAT_ALONE)


class TestHourUrl:
    def test_the_month_is_zero_based(self) -> None:
        # January is 00 and December is 11 in the path, while the day and hour are not.
        # Reading this wrong shifts every request by a month, and the feed answers with
        # a valid file for the wrong period rather than an error.
        assert hour_url("EURUSD", datetime(2025, 1, 2, 3, tzinfo=UTC)) == (
            f"{FEED_BASE_URL}/EURUSD/2025/00/02/03h_ticks.bi5"
        )
        assert hour_url("EURUSD", datetime(2025, 12, 31, 23, tzinfo=UTC)) == (
            f"{FEED_BASE_URL}/EURUSD/2025/11/31/23h_ticks.bi5"
        )

    def test_march_is_02(self) -> None:
        assert hour_url("EURUSD", HOUR) == f"{FEED_BASE_URL}/EURUSD/2025/02/05/10h_ticks.bi5"

    def test_the_base_url_is_overridable(self) -> None:
        assert hour_url("EURUSD", HOUR, base_url="http://localhost:8080/feed/").startswith(
            "http://localhost:8080/feed/EURUSD/"
        )

    @pytest.mark.parametrize("symbol", ["eurusd", "EUR/USD", "", "../../etc/passwd", "EUR USD"])
    def test_implausible_symbols_are_refused(self, symbol: str) -> None:
        # Also keeps a caller supplied string out of the URL path unchecked.
        with pytest.raises(DomainError, match="not a Dukascopy instrument code"):
            hour_url(symbol, HOUR)

    def test_a_naive_hour_is_refused(self) -> None:
        with pytest.raises(DomainError, match="timezone aware"):
            hour_url("EURUSD", datetime(2025, 3, 5, 10))  # noqa: DTZ001

    @pytest.mark.parametrize(
        "instant",
        [
            datetime(2025, 3, 5, 10, 30, tzinfo=UTC),
            datetime(2025, 3, 5, 10, 0, 1, tzinfo=UTC),
            datetime(2025, 3, 5, 10, 0, 0, 1, tzinfo=UTC),
        ],
    )
    def test_an_instant_off_the_hour_is_refused(self, instant: datetime) -> None:
        with pytest.raises(DomainError, match="on the hour"):
            hour_url("EURUSD", instant)

    def test_an_hour_given_in_another_zone_resolves_to_the_same_file(self) -> None:
        # 11:00 Berlin in March is 10:00 UTC, and the feed is indexed in UTC. The hour
        # must be on the hour in UTC, not in whatever zone the caller happens to hold.
        berlin = datetime(2025, 3, 5, 11, tzinfo=ZoneInfo("Europe/Berlin"))
        assert hour_url("EURUSD", berlin) == hour_url("EURUSD", HOUR)

    def test_an_hour_that_is_only_on_the_hour_elsewhere_is_refused(self) -> None:
        # 10:00 in Kolkata is 04:30 UTC, which no .bi5 file starts at.
        with pytest.raises(DomainError, match="on the hour"):
            hour_url("EURUSD", datetime(2025, 3, 5, 10, tzinfo=ZoneInfo("Asia/Kolkata")))


class TestDecodeHour:
    def test_a_record_decodes_field_for_field(self) -> None:
        payload = bi5(record(299, 107123, 107115, 5.04, 0.9))
        assert decode_hour(payload, hour=HOUR, digits=EURUSD_DIGITS) == (
            DukascopyTick(
                ts=datetime(2025, 3, 5, 10, 0, 0, 299_000, tzinfo=UTC),
                bid=Decimal("1.07115"),
                ask=Decimal("1.07123"),
                bid_volume=Decimal("0.89999997615814208984375"),
                ask_volume=Decimal("5.03999996185302734375"),
            ),
        )

    def test_prices_are_scaled_exactly(self) -> None:
        tick = decode_hour(
            bi5(record(0, 107123, 107115, 1.0, 1.0)), hour=HOUR, digits=EURUSD_DIGITS
        )[0]
        # Not merely equal: the same number of decimal places, because a price that
        # gains or loses a digit no longer matches what the feed published.
        assert tick.bid.as_tuple() == Decimal("1.07115").as_tuple()
        assert tick.ask * 100000 == Decimal(107123)

    def test_digits_are_not_assumed(self) -> None:
        # The identical bytes are a JPY pair at three digits. Defaulting to five would
        # misprice it by a factor of a hundred without anything looking wrong.
        payload = bi5(record(0, 15123, 15121, 1.0, 1.0))
        assert decode_hour(payload, hour=HOUR, digits=JPY_DIGITS)[0].bid == Decimal("15.121")
        assert decode_hour(payload, hour=HOUR, digits=EURUSD_DIGITS)[0].bid == Decimal("0.15121")

    def test_zero_digits_are_allowed(self) -> None:
        assert decode_hour(bi5(record(0, 7, 6, 1.0, 1.0)), hour=HOUR, digits=0)[0].bid == Decimal(6)

    @pytest.mark.parametrize("digits", [-1, 13])
    def test_implausible_digits_are_refused(self, digits: int) -> None:
        with pytest.raises(DomainError, match="digits must be between"):
            decode_hour(bi5(record(0, 7, 6, 1.0, 1.0)), hour=HOUR, digits=digits)

    def test_volumes_keep_the_exact_binary32_value(self) -> None:
        # 0.9 has no exact binary representation. The expansion below is the exact
        # value of the 32 bit pattern the feed sent, and it converts back to that same
        # pattern, which is what bit exact means for a float field.
        tick = decode_hour(bi5(record(0, 2, 1, 0.9, 0.9)), hour=HOUR, digits=0)[0]
        assert tick.bid_volume == Decimal("0.89999997615814208984375")
        assert struct.pack(">f", float(tick.bid_volume)) == struct.pack(">f", 0.9)

    def test_timestamps_are_offsets_from_the_hour(self) -> None:
        ticks = decode_hour(
            bi5(record(0, 2, 1, 1.0, 1.0), record(3_599_999, 2, 1, 1.0, 1.0)),
            hour=HOUR,
            digits=0,
        )
        assert ticks[0].ts == HOUR
        assert ticks[-1].ts == HOUR + timedelta(milliseconds=3_599_999)

    def test_records_keep_the_order_the_file_stores(self) -> None:
        ticks = decode_hour(
            bi5(*(record(index * 1000, 2, 1, 1.0, 1.0) for index in range(5))),
            hour=HOUR,
            digits=0,
        )
        assert [tick.ts for tick in ticks] == sorted(tick.ts for tick in ticks)
        assert len(ticks) == 5

    def test_an_empty_hour_is_no_ticks_not_an_error(self) -> None:
        # Every hour of every weekend is served as a zero length body.
        assert decode_hour(b"", hour=HOUR, digits=EURUSD_DIGITS) == ()

    def test_a_compressed_empty_file_is_no_ticks(self) -> None:
        assert decode_hour(bi5(), hour=HOUR, digits=EURUSD_DIGITS) == ()


class TestRejectingWhatIsNotABi5:
    def test_an_html_error_page_is_refused(self) -> None:
        # This is the payload the feed actually serves under parallel load, with a 503.
        # Treating it as an empty hour is the failure this reader exists to prevent.
        page = b"<html><head><title>503 Service Unavailable</title></head><body>...</body></html>"
        with pytest.raises(Bi5DecodeError, match="not an LZMA stream"):
            decode_hour(page, hour=HOUR, digits=EURUSD_DIGITS)

    def test_the_error_names_what_arrived(self) -> None:
        with pytest.raises(Bi5DecodeError, match="503"):
            decompress_bi5(b"<html>503 Service Unavailable</html>")

    def test_a_truncated_stream_is_refused(self) -> None:
        payload = bi5(record(0, 2, 1, 1.0, 1.0))
        with pytest.raises(Bi5DecodeError, match="did not decompress"):
            decompress_bi5(payload[: len(payload) // 2])

    def test_a_partial_record_is_refused(self) -> None:
        # Decompresses cleanly, but the byte count is not a multiple of the record
        # size, so the file is not what it claims to be.
        payload = lzma.compress(record(0, 2, 1, 1.0, 1.0)[:-4], format=lzma.FORMAT_ALONE)
        with pytest.raises(Bi5DecodeError, match=f"not a whole number of {RECORD_SIZE} byte"):
            decompress_bi5(payload)

    def test_an_offset_past_the_end_of_the_hour_is_refused(self) -> None:
        with pytest.raises(Bi5DecodeError, match="past the end of it"):
            decode_hour(bi5(record(3_600_000, 2, 1, 1.0, 1.0)), hour=HOUR, digits=0)

    def test_records_going_backwards_are_refused(self) -> None:
        payload = bi5(record(5000, 2, 1, 1.0, 1.0), record(4000, 2, 1, 1.0, 1.0))
        with pytest.raises(Bi5DecodeError, match="goes back in time"):
            decode_hour(payload, hour=HOUR, digits=0)

    def test_repeated_timestamps_are_allowed(self) -> None:
        # Two quotes in the same millisecond is normal, unlike going backwards.
        payload = bi5(record(5000, 2, 1, 1.0, 1.0), record(5000, 3, 1, 1.0, 1.0))
        assert len(decode_hour(payload, hour=HOUR, digits=0)) == 2

    @pytest.mark.parametrize(("ask", "bid"), [(0, 1), (1, 0), (0, 0)])
    def test_a_zero_price_is_refused(self, ask: int, bid: int) -> None:
        with pytest.raises(Bi5DecodeError, match="zero price"):
            decode_hour(bi5(record(0, ask, bid, 1.0, 1.0)), hour=HOUR, digits=EURUSD_DIGITS)

    def test_a_negative_volume_is_refused(self) -> None:
        with pytest.raises(Bi5DecodeError, match="negative bid volume"):
            decode_hour(bi5(record(0, 2, 1, 1.0, -1.0)), hour=HOUR, digits=0)

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_a_non_finite_volume_is_refused(self, value: float) -> None:
        with pytest.raises(Bi5DecodeError, match="must be finite"):
            decode_hour(bi5(record(0, 2, 1, value, 1.0)), hour=HOUR, digits=0)

    def test_a_naive_hour_is_refused(self) -> None:
        with pytest.raises(DomainError, match="timezone aware"):
            decode_hour(b"", hour=datetime(2025, 3, 5, 10), digits=0)  # noqa: DTZ001

    def test_an_hour_off_the_hour_is_refused(self) -> None:
        with pytest.raises(DomainError, match="on the hour"):
            decode_hour(b"", hour=datetime(2025, 3, 5, 10, 30, tzinfo=UTC), digits=0)


class TestCrossedQuotes:
    def test_a_crossed_quote_is_flagged_not_rejected(self) -> None:
        # These occur around releases. Rejecting the hour would discard exactly the
        # periods worth studying, so they are decoded and counted instead.
        tick = decode_hour(bi5(record(0, 107115, 107123, 1.0, 1.0)), hour=HOUR, digits=5)[0]
        assert tick.is_crossed
        assert tick.spread == Decimal("-0.00008")

    def test_a_normal_quote_is_not_crossed(self) -> None:
        tick = decode_hour(bi5(record(0, 107123, 107115, 1.0, 1.0)), hour=HOUR, digits=5)[0]
        assert not tick.is_crossed
        assert tick.spread == Decimal("0.00008")


class TestARecordedHourFromTheFeed:
    """The format claims above, checked against bytes Dukascopy actually served.

    ``tests/marketdata/data/EURUSD_2025-03-05_10h.bi5`` is the unmodified response to
    ``GET https://datafeed.dukascopy.com/datafeed/EURUSD/2025/02/05/10h_ticks.bi5``,
    retrieved on 2026-08-15, sha256
    04dd9dc7f8bcfd96e75bd7e05948c2fa34f3604f068d4cc94f64fc298c454a8c.

    It is checked in rather than fetched, so the suite needs no network and cannot go
    red because a third party is having a bad afternoon. Everything above this class
    round trips against fixtures this module encodes itself, which proves the decoder
    agrees with the encoder and nothing about Dukascopy. This class is the part that
    is evidence.
    """

    @staticmethod
    def payload() -> bytes:
        return RECORDED_HOUR.read_bytes()

    def test_the_hour_decodes_to_the_expected_number_of_ticks(self) -> None:
        ticks = decode_hour(self.payload(), hour=RECORDED_HOUR_START, digits=EURUSD_DIGITS)
        assert len(ticks) == 6678

    def test_the_first_and_last_ticks_match_the_raw_records(self) -> None:
        ticks = decode_hour(self.payload(), hour=RECORDED_HOUR_START, digits=EURUSD_DIGITS)
        assert ticks[0] == DukascopyTick(
            ts=RECORDED_HOUR_START + timedelta(milliseconds=299),
            bid=Decimal("1.07115"),
            ask=Decimal("1.07123"),
            bid_volume=Decimal("0.89999997615814208984375"),
            ask_volume=Decimal("5.03999996185302734375"),
        )
        assert ticks[-1] == DukascopyTick(
            ts=RECORDED_HOUR_START + timedelta(milliseconds=3_599_854),
            bid=Decimal("1.06969"),
            ask=Decimal("1.06972"),
            bid_volume=Decimal("2.70000004768371582031250"),
            ask_volume=Decimal("0.89999997615814208984375"),
        )

    def test_every_tick_falls_inside_the_hour_and_moves_forward(self) -> None:
        ticks = decode_hour(self.payload(), hour=RECORDED_HOUR_START, digits=EURUSD_DIGITS)
        assert all(
            RECORDED_HOUR_START <= tick.ts < RECORDED_HOUR_START + HOUR_LENGTH for tick in ticks
        )
        assert [tick.ts for tick in ticks] == sorted(tick.ts for tick in ticks)

    def test_the_decoded_hour_re_encodes_to_the_identical_bytes(self) -> None:
        # The strongest statement available about losslessness: every field of all 6678
        # records is converted back and compared against the file byte for byte. If any
        # price were rounded or any volume normalised to something tidier, this differs.
        decompressed = decompress_bi5(self.payload())
        ticks = decode_hour(self.payload(), hour=RECORDED_HOUR_START, digits=EURUSD_DIGITS)
        re_encoded = b"".join(
            struct.pack(
                ">IIIff",
                round((tick.ts - RECORDED_HOUR_START).total_seconds() * 1000),
                int(tick.ask.scaleb(EURUSD_DIGITS)),
                int(tick.bid.scaleb(EURUSD_DIGITS)),
                float(tick.ask_volume),
                float(tick.bid_volume),
            )
            for tick in ticks
        )
        assert re_encoded == decompressed

    def test_the_prices_are_a_plausible_eurusd(self) -> None:
        # A guard against decoding something self consistent but wrong, such as bid and
        # ask transposed at the right scale, or the right integers at the wrong one.
        ticks = decode_hour(self.payload(), hour=RECORDED_HOUR_START, digits=EURUSD_DIGITS)
        assert all(Decimal("1.0") < tick.bid < Decimal("1.2") for tick in ticks)
        crossed = [tick for tick in ticks if tick.is_crossed]
        assert crossed == []

    def test_the_wrong_digits_produce_a_price_no_check_would_pass(self) -> None:
        # The counterpart to the test above: nothing in the file says where the decimal
        # point goes, so the registry value is load bearing and a wrong one is silent.
        ticks = decode_hour(self.payload(), hour=RECORDED_HOUR_START, digits=JPY_DIGITS)
        assert ticks[0].bid == Decimal("107.115")
