"""The Bybit public WebSocket stream, with reconnection that resynchronises state.

What this has to survive is not a clean shutdown but a silent one: a socket that stays
open and stops delivering. Bybit cuts an idle connection after ten minutes, so a
recorder that only reacts to a closed socket can lose ten minutes of a market that
never stops trading, and no historical source exists to fill that in afterwards. Every
read therefore has a deadline, and a deadline that passes is a dead connection even
though nothing was raised.

The application level ping is what makes the deadline usable. Without it, silence is
ambiguous: a quiet market and a broken socket look identical. Bybit answers a ping with
a pong, so a connection that is alive produces traffic on its own schedule, and any
silence longer than the timeout is a fault rather than a lull.

Reconnection is exponential with full jitter. The jitter matters more than it looks:
several instruments reconnecting on one schedule arrive together, are throttled
together, and back off together, which turns a brief outage into a synchronised herd
that sustains it.

State is discarded on every reconnect. The book is rebuilt from the next snapshot and
nothing is emitted until one arrives. Carrying a book across a gap of unknown length
produces quotes that look continuous and are not, and that is the one corruption
nobody can detect after the fact.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol, final

from tradingsys.venues.bybit.book import BookState, ResyncRequiredError, topic_for
from tradingsys.venues.bybit.instruments import VENUE
from tradingsys.venues.errors import VenueConnectivityError, VenueResponseError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping

    from tradingsys.core.instrument import InstrumentId
    from tradingsys.venues.models import Quote

__all__ = [
    "BybitPublicStream",
    "StreamStats",
    "WebSocketLike",
]

PING: Final = json.dumps({"op": "ping"})


class WebSocketLike(Protocol):
    """The part of a WebSocket this module uses.

    Narrow on purpose. The tests drive a scripted socket, because a reconnection test
    that needs a live venue is a test nobody runs on the day it would have helped.
    """

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


type Connect = Callable[[str], Awaitable[WebSocketLike]]
type WaitFor = Callable[[Coroutine[Any, Any, str | bytes], float], Awaitable[str | bytes]]


@final
@dataclass(slots=True)
class StreamStats:
    """Counters a supervisor reads to tell a quiet market from a broken feed."""

    connections: int = 0
    quotes: int = 0
    pings_sent: int = 0
    resyncs: int = 0
    rejected_messages: int = 0
    silence_timeouts: int = 0
    failures: int = 0
    last_error: str | None = None
    """Type and message of the failure that ended the last connection.

    Kept because the reconnect loop swallows the exception by design, and an operator
    looking at a stream that reconnects every thirty seconds needs to know why without
    reading through a day of logs.
    """
    reconnect_delays: list[float] = field(default_factory=list)


@final
class BybitPublicStream:
    """Top of book quotes for a set of instruments, reconnecting as needed.

    Iterating yields quotes indefinitely. Disconnections are handled inside the
    iteration rather than surfaced, because a consumer that has to reconnect the
    stream itself ends up reimplementing this loop with less care.
    """

    __slots__ = (
        "_backoff",
        "_books",
        "_connect",
        "_jitter",
        "_max_backoff",
        "_monotonic",
        "_ping_interval",
        "_receive_timeout",
        "_sleep",
        "_stats",
        "_symbols",
        "_url",
        "_wait_for",
    )

    def __init__(
        self,
        url: str,
        symbols: Mapping[str, InstrumentId],
        *,
        connect: Connect,
        ping_interval_seconds: float = 20.0,
        receive_timeout_seconds: float = 30.0,
        backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
        jitter: Callable[[float], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
        wait_for: WaitFor | None = None,
    ) -> None:
        """
        Args:
            url: Public stream URL, including the category path.
            symbols: Venue symbol to instrument id. One subscription per entry.
            connect: Opens a connection. Injected so reconnection can be tested against
                a scripted socket rather than a live venue.
            ping_interval_seconds: Bybit documents 20 seconds.
            receive_timeout_seconds: Silence longer than this is a dead connection.
                Must exceed the ping interval, or a healthy connection is torn down
                before its own pong can arrive.
            backoff_seconds: First reconnect delay, doubled per consecutive failure.
            max_backoff_seconds: Ceiling on that doubling.
            jitter: Applied to each delay. Defaults to full jitter, uniform in
                ``[0, delay]``.
            sleep: Awaitable delay, injected for tests.
            monotonic: Clock for the ping and silence deadlines, injected for tests.
            wait_for: Applies a timeout to a receive. Injected so a dead socket can be
                simulated without the suite waiting out a real timeout.

        Raises:
            VenueResponseError: No symbols, or a receive timeout that does not exceed
                the ping interval.
        """
        if not symbols:
            raise VenueResponseError(VENUE, "a stream needs at least one symbol to subscribe to")
        if receive_timeout_seconds <= ping_interval_seconds:
            raise VenueResponseError(
                VENUE,
                f"receive_timeout_seconds ({receive_timeout_seconds}) must exceed "
                f"ping_interval_seconds ({ping_interval_seconds}), otherwise a healthy "
                f"connection is declared dead before the pong it asked for can arrive",
            )
        self._url = url
        self._symbols = dict(symbols)
        self._connect = connect
        self._ping_interval = ping_interval_seconds
        self._receive_timeout = receive_timeout_seconds
        self._backoff = backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._jitter: Callable[[float], float] = jitter or (
            # Jitter, not cryptography.
            lambda delay: random.uniform(0, delay)
        )
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._monotonic: Callable[[], float] = monotonic or time.monotonic
        self._wait_for: WaitFor = wait_for or _default_wait_for
        self._books: dict[str, BookState] = {}
        self._stats = StreamStats()

    @property
    def stats(self) -> StreamStats:
        return self._stats

    async def quotes(self, *, max_connections: int | None = None) -> AsyncIterator[Quote]:
        """Yield quotes, reconnecting after any failure.

        Args:
            max_connections: Stop after this many connections. A test affordance only:
                in production the stream runs until cancelled, and an iterator that
                ends on its own would stop the recording without anyone being told.
        """
        attempt = 0
        while max_connections is None or self._stats.connections < max_connections:
            self._stats.connections += 1
            try:
                async for quote in self._one_connection():
                    attempt = 0
                    yield quote
            except Exception as error:
                # Deliberately every exception. A dropped socket, a missed deadline, a
                # resync, and a malformed frame all have one remedy: a new connection
                # and a fresh snapshot. Narrowing this was tried and was wrong, because
                # a websocket library signals a closed connection with its own
                # exception type, which is not an OSError, and the recorder would have
                # died on the first disconnection with the list that looked complete.
                # CancelledError is a BaseException and still passes through, so
                # shutdown is unaffected.
                self._stats.failures += 1
                self._stats.last_error = f"{type(error).__name__}: {error}"
            for book in self._books.values():
                book.reset()
            delay = self._jitter(min(self._max_backoff, self._backoff * (2**attempt)))
            self._stats.reconnect_delays.append(delay)
            attempt += 1
            await self._sleep(delay)

    async def _one_connection(self) -> AsyncIterator[Quote]:
        socket = await self._connect(self._url)
        try:
            await socket.send(
                json.dumps(
                    {"op": "subscribe", "args": [topic_for(symbol) for symbol in self._symbols]}
                )
            )
            started = self._monotonic()
            last_message = started
            last_ping = started
            while True:
                now = self._monotonic()
                if now - last_message >= self._receive_timeout:
                    self._stats.silence_timeouts += 1
                    raise VenueConnectivityError(
                        VENUE,
                        f"nothing received for {now - last_message:.1f} seconds. A ping was "
                        f"sent and not answered, and this market does not close, so the "
                        f"connection is dead rather than quiet.",
                    )
                if now - last_ping >= self._ping_interval:
                    await socket.send(PING)
                    self._stats.pings_sent += 1
                    last_ping = now
                    continue
                budget = min(
                    self._receive_timeout - (now - last_message),
                    self._ping_interval - (now - last_ping),
                )
                try:
                    raw = await self._wait_for(socket.recv(), budget)
                except TimeoutError:
                    # Not a failure by itself: it just means the next deadline came
                    # first. The loop re-evaluates which one it was.
                    continue
                last_message = self._monotonic()
                quote = self._handle(raw)
                if quote is not None:
                    self._stats.quotes += 1
                    yield quote
        finally:
            with contextlib.suppress(Exception):
                await socket.close()

    def _handle(self, raw: str | bytes) -> Quote | None:
        message = _decode(raw)
        topic = message.get("topic")
        if not isinstance(topic, str):
            # Control frames: subscription acknowledgements and pongs. A refused
            # subscription is reported here rather than by closing the socket, so it
            # has to be raised or the connection sits open delivering nothing at all.
            if message.get("op") == "subscribe" and message.get("success") is False:
                raise VenueResponseError(VENUE, f"subscription refused: {message.get('ret_msg')!r}")
            return None
        book = self._book_for(topic)
        try:
            return book.apply(message)
        except ResyncRequiredError:
            self._stats.resyncs += 1
            raise
        except VenueResponseError:
            self._stats.rejected_messages += 1
            raise

    def _book_for(self, topic: str) -> BookState:
        book = self._books.get(topic)
        if book is None:
            symbol = topic.rsplit(".", 1)[-1]
            instrument_id = self._symbols.get(symbol)
            if instrument_id is None:
                raise VenueResponseError(
                    VENUE, f"received topic {topic!r}, which was never subscribed to"
                )
            book = BookState(instrument_id=instrument_id)
            self._books[topic] = book
        return book


async def _default_wait_for(
    awaitable: Coroutine[Any, Any, str | bytes], timeout: float
) -> str | bytes:
    return await asyncio.wait_for(awaitable, timeout=timeout)


def _decode(raw: str | bytes) -> Mapping[str, Any]:
    try:
        message = json.loads(raw)
    except ValueError as error:
        raise VenueResponseError(VENUE, f"stream sent a frame that is not JSON: {raw!r}") from error
    if not isinstance(message, dict):
        raise VenueResponseError(VENUE, f"stream sent {type(message).__name__}, not an object")
    return message
