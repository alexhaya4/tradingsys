"""Tests for the public stream's connection loop.

The socket is scripted rather than real. Reconnection behaviour tested against a live
venue is behaviour tested on a good day, and the cases that matter here are the bad
ones: a socket that closes, a socket that goes silent while staying open, and a
subscription the venue refuses.

Time is injected for the same reason as in the limiter tests. A backoff schedule
asserted against the wall clock takes a minute to verify and is flaky on a loaded
machine.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tradingsys.core.instrument import InstrumentId
from tradingsys.venues.bybit.book import topic_for
from tradingsys.venues.bybit.stream import PING, BybitPublicStream
from tradingsys.venues.errors import VenueResponseError

BTC = InstrumentId(venue="bybit", symbol="BTC/USDT")
ETH = InstrumentId(venue="bybit", symbol="ETH/USDT")
SYMBOLS = {"BTCUSDT": BTC}
URL = "wss://stream-testnet.bybit.com/v5/public/linear"


def frame(price: str = "63033.3", *, update_id: int = 100, symbol: str = "BTCUSDT") -> str:
    ask = str(float(price) + 0.1)
    return json.dumps(
        {
            "topic": topic_for(symbol),
            "ts": 1786870654169,
            "type": "snapshot",
            "data": {
                "s": symbol,
                "b": [[price, "1.5"]],
                "a": [[ask, "2.5"]],
                "u": update_id,
                "seq": 1,
            },
            "cts": 1786870654167,
        }
    )


class SocketClosedError(Exception):
    """The scripted socket ran out of frames, standing in for a dropped connection."""


class ScriptedSocket:
    """Replays a list of frames, then fails the way a closed socket does."""

    def __init__(self, frames: list[str | BaseException]) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._frames = list(frames)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        if not self._frames:
            raise SocketClosedError("no more frames")
        item = self._frames.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


class Connector:
    """Hands out a scripted socket per connection, and remembers them."""

    def __init__(self, *scripts: list[str | BaseException]) -> None:
        self.sockets: list[ScriptedSocket] = []
        self.urls: list[str] = []
        self._scripts = list(scripts)

    async def __call__(self, url: str) -> ScriptedSocket:
        self.urls.append(url)
        script = self._scripts.pop(0) if len(self._scripts) > 1 else self._scripts[0]
        socket = ScriptedSocket(list(script))
        self.sockets.append(socket)
        return socket


class Timeline:
    """A clock that only moves when the stream sleeps, plus a scriptable receive."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def stream(
    connector: Connector,
    timeline: Timeline,
    *,
    symbols: dict[str, InstrumentId] | None = None,
    **kwargs: Any,
) -> BybitPublicStream:
    return BybitPublicStream(
        URL,
        symbols if symbols is not None else SYMBOLS,
        connect=connector,
        jitter=lambda delay: delay,  # asserted schedules must be schedules, not samples
        sleep=timeline.sleep,
        monotonic=timeline.monotonic,
        **kwargs,
    )


async def collect(source: BybitPublicStream, count: int, *, max_connections: int = 5) -> list[Any]:
    quotes = []
    async for quote in source.quotes(max_connections=max_connections):
        quotes.append(quote)
        if len(quotes) == count:
            break
    return quotes


class TestSubscription:
    async def test_it_subscribes_to_every_symbol_on_connect(self) -> None:
        connector = Connector([frame()])
        source = stream(connector, Timeline(), symbols={"BTCUSDT": BTC, "ETHUSDT": ETH})
        await collect(source, 1)
        sent = json.loads(connector.sockets[0].sent[0])
        assert sent["op"] == "subscribe"
        assert sent["args"] == ["orderbook.1.BTCUSDT", "orderbook.1.ETHUSDT"]

    async def test_it_connects_to_the_configured_url(self) -> None:
        connector = Connector([frame()])
        await collect(stream(connector, Timeline()), 1)
        assert connector.urls == [URL]

    async def test_a_refused_subscription_is_not_ignored(self) -> None:
        # Bybit reports this in a frame rather than by closing the socket, so a stream
        # that skips control frames sits there forever receiving nothing.
        refusal = json.dumps({"op": "subscribe", "success": False, "ret_msg": "unknown topic"})
        connector = Connector([refusal, frame()], [frame()])
        source = stream(connector, Timeline())
        quotes = await collect(source, 1)
        assert len(quotes) == 1
        assert source.stats.connections == 2  # it reconnected rather than carrying on

    async def test_an_acknowledgement_yields_no_quote(self) -> None:
        ack = json.dumps({"op": "subscribe", "success": True, "ret_msg": "", "conn_id": "x"})
        connector = Connector([ack, frame()])
        quotes = await collect(stream(connector, Timeline()), 1)
        assert len(quotes) == 1

    async def test_an_unsubscribed_topic_is_refused(self) -> None:
        connector = Connector([frame(symbol="SOLUSDT"), frame()], [frame()])
        source = stream(connector, Timeline())
        await collect(source, 1)
        assert source.stats.connections == 2

    def test_a_stream_with_no_symbols_is_refused(self) -> None:
        with pytest.raises(VenueResponseError, match="at least one symbol"):
            stream(Connector([frame()]), Timeline(), symbols={})

    def test_a_receive_timeout_inside_the_ping_interval_is_refused(self) -> None:
        # Otherwise a healthy connection is declared dead before the pong it asked for
        # has any chance to arrive, and the stream reconnects in a loop.
        with pytest.raises(VenueResponseError, match="must exceed"):
            stream(
                Connector([frame()]),
                Timeline(),
                ping_interval_seconds=30.0,
                receive_timeout_seconds=20.0,
            )


class TestQuotes:
    async def test_quotes_come_out_in_order(self) -> None:
        connector = Connector([frame("100"), frame("101", update_id=101)])
        quotes = await collect(stream(connector, Timeline()), 2)
        assert [str(quote.bid) for quote in quotes] == ["100", "101"]

    async def test_the_quote_count_is_reported(self) -> None:
        connector = Connector([frame(), frame(update_id=101)])
        source = stream(connector, Timeline())
        await collect(source, 2)
        assert source.stats.quotes == 2


class TestReconnection:
    async def test_a_dropped_socket_reconnects_and_keeps_delivering(self) -> None:
        connector = Connector([frame("100")], [frame("101", update_id=101)])
        source = stream(connector, Timeline())
        quotes = await collect(source, 2)
        assert [str(quote.bid) for quote in quotes] == ["100", "101"]
        assert source.stats.connections == 2

    async def test_the_old_socket_is_closed(self) -> None:
        connector = Connector([frame("100")], [frame("101", update_id=101)])
        await collect(stream(connector, Timeline()), 2)
        assert connector.sockets[0].closed

    async def test_the_backoff_doubles_and_is_capped(self) -> None:
        timeline = Timeline()
        # Every connection fails immediately, so the delays are consecutive failures.
        connector = Connector([SocketClosedError("dropped")])
        source = stream(connector, timeline, backoff_seconds=1.0, max_backoff_seconds=8.0)
        async for _ in source.quotes(max_connections=6):
            pass
        assert source.stats.reconnect_delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]

    async def test_a_successful_quote_resets_the_backoff(self) -> None:
        # Two failures back off to two seconds. The connection that delivers a quote
        # clears that history, so the next failure starts at one second again.
        # Without the reset, an hour of healthy running followed by one blip inherits
        # whatever delay start up happened to reach.
        timeline = Timeline()
        connector = Connector(
            [SocketClosedError("x")],
            [SocketClosedError("y")],
            [frame()],
            [SocketClosedError("z")],
        )
        source = stream(connector, timeline, backoff_seconds=1.0)
        async for _ in source.quotes(max_connections=4):
            pass
        assert source.stats.reconnect_delays == [1.0, 2.0, 1.0, 2.0]

    async def test_the_reason_for_the_last_failure_is_kept(self) -> None:
        # The loop swallows the exception on purpose, so an operator watching a stream
        # reconnect every few seconds needs the reason somewhere other than the logs.
        connector = Connector([SocketClosedError("connection reset by peer")])
        source = stream(connector, Timeline())
        async for _ in source.quotes(max_connections=2):
            pass
        assert source.stats.failures == 2
        assert source.stats.last_error is not None
        assert "connection reset by peer" in source.stats.last_error

    async def test_the_book_is_rebuilt_after_a_reconnect(self) -> None:
        # A delta arriving on a fresh connection has nothing to apply to, so the
        # stream must reconnect again rather than emit a quote from a partial book.
        delta = json.dumps(
            {
                "topic": topic_for("BTCUSDT"),
                "ts": 1786870654169,
                "type": "delta",
                "data": {"s": "BTCUSDT", "b": [["100", "1"]], "a": [], "u": 101, "seq": 1},
                "cts": 1786870654167,
            }
        )
        connector = Connector([frame("100")], [delta, frame("200", update_id=200)], [frame("300")])
        source = stream(connector, Timeline())
        quotes = await collect(source, 2, max_connections=4)
        assert [str(quote.bid) for quote in quotes] == ["100", "300"]
        assert source.stats.resyncs == 1

    async def test_a_malformed_frame_reconnects_rather_than_crashing(self) -> None:
        connector = Connector(["not json at all", frame()], [frame("200")])
        source = stream(connector, Timeline())
        quotes = await collect(source, 1)
        assert str(quotes[0].bid) == "200"
        assert source.stats.rejected_messages == 0  # decode failed before any book saw it

    async def test_a_json_array_frame_reconnects(self) -> None:
        # Valid JSON, wrong shape. Reading a topic out of a list would fail somewhere
        # deeper with a message about list indices.
        connector = Connector(["[1, 2, 3]"], [frame("200")])
        source = stream(connector, Timeline())
        quotes = await collect(source, 1)
        assert str(quotes[0].bid) == "200"
        assert source.stats.last_error is not None
        assert "not an object" in source.stats.last_error

    async def test_a_crossed_quote_costs_the_connection_not_the_recorder(self) -> None:
        crossed = json.dumps(
            {
                "topic": topic_for("BTCUSDT"),
                "ts": 1786870654169,
                "type": "snapshot",
                "data": {
                    "s": "BTCUSDT",
                    "b": [["101", "1"]],
                    "a": [["100", "1"]],
                    "u": 100,
                    "seq": 1,
                },
                "cts": 1786870654167,
            }
        )
        connector = Connector([crossed], [frame("200")])
        source = stream(connector, Timeline())
        quotes = await collect(source, 1)
        assert str(quotes[0].bid) == "200"
        assert source.stats.rejected_messages == 1


class TestSilenceAndPings:
    async def test_a_socket_that_goes_silent_is_treated_as_dead(self) -> None:
        # The failure mode the deadline exists for: nothing is raised, the socket is
        # open, and no data arrives. Ten minutes of this is ten minutes of a market
        # that has no historical source to backfill from.
        timeline = Timeline()

        async def wait_for(awaitable: Any, timeout: float) -> str | bytes:
            awaitable.close()
            timeline.now += timeout
            raise TimeoutError

        connector = Connector([frame()])
        source = BybitPublicStream(
            URL,
            SYMBOLS,
            connect=connector,
            ping_interval_seconds=20.0,
            receive_timeout_seconds=30.0,
            jitter=lambda delay: delay,
            sleep=timeline.sleep,
            monotonic=timeline.monotonic,
            wait_for=wait_for,
        )
        async for _ in source.quotes(max_connections=1):
            pass
        assert source.stats.silence_timeouts == 1
        assert source.stats.pings_sent >= 1

    async def test_a_ping_is_sent_when_the_interval_elapses(self) -> None:
        # The ping is what makes silence meaningful: a live connection answers it, so
        # a quiet market still produces traffic.
        timeline = Timeline()
        pinged: list[str] = []

        async def wait_for(awaitable: Any, timeout: float) -> str | bytes:
            awaitable.close()
            timeline.now += timeout
            raise TimeoutError

        connector = Connector([frame()])
        source = BybitPublicStream(
            URL,
            SYMBOLS,
            connect=connector,
            ping_interval_seconds=20.0,
            receive_timeout_seconds=30.0,
            jitter=lambda delay: delay,
            sleep=timeline.sleep,
            monotonic=timeline.monotonic,
            wait_for=wait_for,
        )
        async for _ in source.quotes(max_connections=1):
            pass
        pinged = [message for message in connector.sockets[0].sent if message == PING]
        assert pinged == [PING]
