"""Tests for decoding the orderbook.1 stream.

``data/ws_orderbook_linear.jsonl`` is twelve consecutive frames captured from
``wss://stream.bybit.com/v5/public/linear`` on 2026-08-16, subscribed to BTCUSDT and
ETHUSDT, including the subscription acknowledgement. It is the evidence for what the
venue actually sends; the constructed messages below cover what it documents and what
it would send if something went wrong.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tradingsys.core.instrument import InstrumentId
from tradingsys.venues.bybit.book import BookState, ResyncRequiredError, topic_for
from tradingsys.venues.errors import VenueResponseError

BTC = InstrumentId(venue="bybit", symbol="BTC/USDT")
RECORDED = Path(__file__).parent / "data" / "ws_orderbook_linear.jsonl"


def frames() -> list[dict[str, Any]]:
    return [json.loads(line) for line in RECORDED.read_text().splitlines() if line.strip()]


def message(
    *,
    kind: str = "snapshot",
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
    update_id: int = 100,
    cts: int = 1786870654167,
) -> dict[str, Any]:
    return {
        "topic": topic_for("BTCUSDT"),
        "ts": cts + 2,
        "type": kind,
        "data": {
            "s": "BTCUSDT",
            "b": [["63033.3", "14.086"]] if bids is None else bids,
            "a": [["63033.4", "2.486"]] if asks is None else asks,
            "u": update_id,
            "seq": 770143423738,
        },
        "cts": cts,
    }


@pytest.fixture
def book() -> BookState:
    return BookState(instrument_id=BTC)


class TestTheRecordedStream:
    def test_every_recorded_data_frame_is_a_snapshot(self) -> None:
        # Observed, and the reason the delta path has no coverage from real data: at
        # depth one this venue sends complete tops, not increments.
        kinds = {frame["type"] for frame in frames() if "topic" in frame}
        assert kinds == {"snapshot"}

    def test_the_recorded_update_ids_increment_by_one(self) -> None:
        for symbol in ("BTCUSDT", "ETHUSDT"):
            ids = [
                frame["data"]["u"] for frame in frames() if frame.get("topic") == topic_for(symbol)
            ]
            assert ids == list(range(ids[0], ids[0] + len(ids)))

    def test_a_recorded_frame_decodes_to_its_quote(self, book: BookState) -> None:
        first = next(frame for frame in frames() if frame.get("topic") == topic_for("BTCUSDT"))
        quote = book.apply(first)
        assert quote is not None
        assert quote.bid == Decimal("63033.3")
        assert quote.ask == Decimal("63033.4")
        assert quote.bid_size == Decimal("14.086")
        assert quote.ask_size == Decimal("2.486")
        assert quote.instrument_id == BTC

    def test_prices_keep_the_venues_own_digits(self, book: BookState) -> None:
        # Parsed from the venue's decimal strings, so 14.086 is 14.086 and not
        # 14.085999999999999.
        first = next(frame for frame in frames() if frame.get("topic") == topic_for("BTCUSDT"))
        quote = book.apply(first)
        assert quote is not None
        assert str(quote.bid_size) == "14.086"

    def test_the_control_frame_carries_no_topic(self) -> None:
        control = [frame for frame in frames() if "topic" not in frame]
        assert len(control) == 1
        assert control[0]["op"] == "subscribe"
        assert control[0]["success"] is True


class TestTimestamps:
    def test_the_matching_engine_clock_is_preferred(self, book: BookState) -> None:
        # cts is stamped by the engine, ts when the message was pushed. The engine's
        # time is what lines these quotes up against trades and other venues.
        quote = book.apply(message(cts=1786870654167))
        assert quote is not None
        assert quote.ts == datetime(2026, 8, 16, 8, 57, 34, 167000, tzinfo=UTC)

    def test_the_push_time_is_used_when_the_engine_time_is_absent(self, book: BookState) -> None:
        payload = message()
        del payload["cts"]
        quote = book.apply(payload)
        assert quote is not None
        assert quote.ts == datetime(2026, 8, 16, 8, 57, 34, 169000, tzinfo=UTC)

    def test_a_message_with_no_timestamp_is_refused(self, book: BookState) -> None:
        payload = message()
        del payload["cts"]
        del payload["ts"]
        with pytest.raises(VenueResponseError, match="no usable timestamp"):
            book.apply(payload)


class TestSnapshotsAndDeltas:
    def test_a_snapshot_replaces_the_book(self, book: BookState) -> None:
        book.apply(message(bids=[["100", "1"]], asks=[["101", "1"]]))
        quote = book.apply(message(bids=[["200", "2"]], asks=[["201", "2"]], update_id=101))
        assert quote is not None
        assert (quote.bid, quote.ask) == (Decimal(200), Decimal(201))

    def test_a_delta_updates_a_level(self, book: BookState) -> None:
        book.apply(message(bids=[["100", "1"]], asks=[["101", "1"]]))
        quote = book.apply(message(kind="delta", bids=[["100", "5"]], asks=[], update_id=101))
        assert quote is not None
        assert quote.bid_size == Decimal(5)

    def test_a_zero_size_deletes_the_level(self, book: BookState) -> None:
        # Documented behaviour. Keeping a zero sized level would leave a price in the
        # book that nobody is quoting.
        book.apply(message(bids=[["100", "1"], ["99", "3"]], asks=[["101", "1"]]))
        quote = book.apply(message(kind="delta", bids=[["100", "0"]], asks=[], update_id=101))
        assert quote is not None
        assert quote.bid == Decimal(99)

    def test_deleting_the_last_level_yields_no_quote(self, book: BookState) -> None:
        # A one sided book has no spread. Inventing the missing side is the only worse
        # option available.
        book.apply(message(bids=[["100", "1"]], asks=[["101", "1"]]))
        assert (
            book.apply(message(kind="delta", bids=[["100", "0"]], asks=[], update_id=101)) is None
        )

    def test_an_update_id_of_one_is_treated_as_a_restart(self, book: BookState) -> None:
        # Bybit reuses u == 1 for a service restart. Applying it as a delta would merge
        # a fresh book into a stale one.
        book.apply(message(bids=[["100", "1"]], asks=[["101", "1"]]))
        quote = book.apply(
            message(kind="delta", bids=[["200", "2"]], asks=[["201", "2"]], update_id=1)
        )
        assert quote is not None
        assert (quote.bid, quote.ask) == (Decimal(200), Decimal(201))

    def test_a_delta_before_any_snapshot_demands_a_resync(self, book: BookState) -> None:
        with pytest.raises(ResyncRequiredError, match="before any snapshot"):
            book.apply(message(kind="delta", update_id=101))

    def test_an_update_id_going_backwards_demands_a_resync(self, book: BookState) -> None:
        book.apply(message(update_id=500))
        with pytest.raises(ResyncRequiredError, match="went backwards"):
            book.apply(message(kind="delta", update_id=499))

    def test_a_reset_book_refuses_deltas_again(self, book: BookState) -> None:
        # What happens on every reconnect: the book is dropped, and the next delta is
        # unusable until a snapshot rebuilds it.
        book.apply(message())
        book.reset()
        assert book.last_update_id is None
        with pytest.raises(ResyncRequiredError):
            book.apply(message(kind="delta", update_id=101))


class TestMalformedMessages:
    def test_an_unknown_message_type_is_refused(self, book: BookState) -> None:
        with pytest.raises(VenueResponseError, match="unknown orderbook message type"):
            book.apply(message(kind="patch"))

    def test_a_missing_data_object_is_refused(self, book: BookState) -> None:
        with pytest.raises(VenueResponseError, match="no data object"):
            book.apply({"topic": topic_for("BTCUSDT"), "type": "snapshot", "cts": 1})

    def test_a_missing_update_id_is_refused(self, book: BookState) -> None:
        payload = message()
        del payload["data"]["u"]
        with pytest.raises(VenueResponseError, match="'u' is missing"):
            book.apply(payload)

    def test_a_side_that_is_not_a_list_is_refused(self, book: BookState) -> None:
        payload = message()
        payload["data"]["b"] = "63033.3"
        with pytest.raises(VenueResponseError, match="side 'b' is missing or not a list"):
            book.apply(payload)

    @pytest.mark.parametrize("level", [["100"], ["100", "1", "extra"], [100, 1], "100"])
    def test_a_malformed_level_is_refused(self, book: BookState, level: object) -> None:
        payload = message()
        payload["data"]["b"] = [level]
        with pytest.raises(VenueResponseError, match="malformed level"):
            book.apply(payload)

    def test_a_crossed_top_of_book_is_rejected_rather_than_stored(self, book: BookState) -> None:
        # A matched exchange should never publish one. If it does, the message is
        # dropped and counted: recording bid above ask would corrupt every spread
        # statistic computed from this data later.
        with pytest.raises(VenueResponseError, match="refusing an unusable quote"):
            book.apply(message(bids=[["101", "1"]], asks=[["100", "1"]]))

    def test_a_zero_price_is_rejected(self, book: BookState) -> None:
        with pytest.raises(VenueResponseError, match="refusing an unusable quote"):
            book.apply(message(bids=[["0", "1"]], asks=[["100", "1"]]))
