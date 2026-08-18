"""The Bybit instrument source: the path that did not exist between two complete parts.

`venues/bybit/instruments.py` mapped a venue payload to an Instrument correctly and
nothing presented those mappings as an `InstrumentSource`, so the registry could not sync
the one venue this system records. These tests cover the edge, and the refusals matter
more than the happy path: a source that returns what resolved is indistinguishable from a
venue that delisted the rest.

Responses are the recorded bodies in `data/`, served through an httpx MockTransport, so
the file runs offline and deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from tradingsys.core.currency import default_registry
from tradingsys.venues.bybit.rest import BybitRestClient
from tradingsys.venues.bybit.source import BybitInstrumentSource
from tradingsys.venues.errors import InstrumentNotFoundError, VenueResponseError
from tradingsys.venues.ratelimit import RateLimiter

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.asyncio

DATA = Path(__file__).parent / "data"
BASE = "https://api-testnet.bybit.com"


def recorded(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((DATA / name).read_text())
    return body


def rows_of(name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = recorded(name)["result"]["list"]
    return rows


def envelope(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"category": "linear", "list": list(rows), "nextPageCursor": ""},
    }


class Venue:
    """Answers instruments-info and tickers from whatever the test supplies."""

    def __init__(
        self,
        instruments: Sequence[dict[str, Any]],
        tickers: Sequence[dict[str, Any]],
    ) -> None:
        self.paths: list[str] = []
        self._instruments = envelope(instruments)
        self._tickers = envelope(tickers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if "instruments-info" in request.url.path:
            return httpx.Response(200, json=self._instruments)
        return httpx.Response(200, json=self._tickers)


def source(venue: Venue, symbols: Sequence[str] = ("ETHUSDT",)) -> BybitInstrumentSource:
    client = BybitRestClient(
        BASE,
        client=httpx.AsyncClient(transport=httpx.MockTransport(venue)),
        limiter=RateLimiter(1000),
    )
    return BybitInstrumentSource(client, symbols, default_registry)


def eth_and_ticker() -> Venue:
    return Venue(rows_of("instruments_linear_ETHUSDT.json"), rows_of("tickers_linear_BTCUSDT.json"))


def ticker_for(symbol: str) -> dict[str, Any]:
    """The recorded ticker, relabelled, since only nextFundingTime is read from it."""
    row = dict(rows_of("tickers_linear_BTCUSDT.json")[0])
    row["symbol"] = symbol
    return row


class TestItResolvesTheConfiguredUniverse:
    async def test_a_definition_comes_back_for_each_symbol(self) -> None:
        venue = Venue(rows_of("instruments_linear_ETHUSDT.json"), [ticker_for("ETHUSDT")])
        instruments = await source(venue).instruments()

        assert len(instruments) == 1
        assert instruments[0].symbol == "ETH/USDT"

    async def test_it_reads_both_endpoints(self) -> None:
        """The definition and the funding anchor come from different calls, and the
        anchor is the only published source of the cycle phase."""
        venue = Venue(rows_of("instruments_linear_ETHUSDT.json"), [ticker_for("ETHUSDT")])
        await source(venue).instruments()

        assert any("instruments-info" in path for path in venue.paths)
        assert any("tickers" in path for path in venue.paths)

    async def test_the_venue_name_is_reported(self) -> None:
        assert source(eth_and_ticker()).venue == "bybit"

    async def test_only_the_configured_symbols_are_returned(self) -> None:
        """The venue lists hundreds and this system trades one."""
        extra = dict(rows_of("instruments_linear_ETHUSDT.json")[0])
        extra["symbol"] = "SOLUSDT"
        venue = Venue(
            [*rows_of("instruments_linear_ETHUSDT.json"), extra],
            [ticker_for("ETHUSDT"), ticker_for("SOLUSDT")],
        )
        instruments = await source(venue, ["ETHUSDT"]).instruments()

        assert [i.venue_symbol for i in instruments] == ["ETHUSDT"]


class TestItRefusesRatherThanReturningLess:
    async def test_an_unlisted_symbol_raises(self) -> None:
        """Returning the ones that resolved is indistinguishable from the venue having
        delisted the rest, and the registry would record that as an absence."""
        venue = Venue(rows_of("instruments_linear_ETHUSDT.json"), [ticker_for("ETHUSDT")])

        with pytest.raises(InstrumentNotFoundError, match="BTCUSDT"):
            await source(venue, ["ETHUSDT", "BTCUSDT"]).instruments()

    async def test_a_symbol_without_a_ticker_raises(self) -> None:
        """Without the anchor the funding cycle phase is unknown, and a perpetual priced
        on a guessed phase is wrong by a whole settlement per overnight hold."""
        venue = Venue(rows_of("instruments_linear_ETHUSDT.json"), [])

        with pytest.raises(VenueResponseError, match="no funding anchor"):
            await source(venue).instruments()

    async def test_nothing_is_returned_when_one_symbol_fails(self) -> None:
        """A partial refresh must not land: the registry would report the missing one as
        drift rather than as a failed fetch."""
        venue = Venue(rows_of("instruments_linear_ETHUSDT.json"), [ticker_for("ETHUSDT")])

        with pytest.raises(InstrumentNotFoundError):
            await source(venue, ["ETHUSDT", "ABSENT"]).instruments()

    async def test_a_spot_payload_is_refused(self) -> None:
        """Spot rows carry no contractType, and this source reads perpetuals only."""
        venue = Venue(rows_of("instruments_spot_ETHUSDT.json"), [ticker_for("ETHUSDT")])

        with pytest.raises(VenueResponseError):
            await source(venue).instruments()

    async def test_a_row_without_a_symbol_does_not_match_anything(self) -> None:
        """It cannot be matched to a requested symbol, so the requested one is reported
        missing by name rather than the payload being reported as malformed."""
        nameless = dict(rows_of("instruments_linear_ETHUSDT.json")[0])
        del nameless["symbol"]
        venue = Venue([nameless], [ticker_for("ETHUSDT")])

        with pytest.raises(InstrumentNotFoundError, match="ETHUSDT"):
            await source(venue).instruments()
