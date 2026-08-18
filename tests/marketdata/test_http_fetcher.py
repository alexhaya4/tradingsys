"""The component that fetches every byte of historical data, previously untested.

It carried zero coverage while being reached in production through `BackfillRunner` and
constructed only by a measurement script CI does not run. The distinction it exists to
make is between an hour the feed genuinely holds nothing for and an hour the fetch
failed on, and getting that backwards writes a hole into history that never existed.

Served by an httpx MockTransport, so this runs offline.
"""

from __future__ import annotations

import httpx
import pytest

from tradingsys.marketdata.backfill import HttpHourFetcher
from tradingsys.venues.errors import VenueConnectivityError, VenueResponseError

pytestmark = pytest.mark.asyncio

URL = "https://datafeed.dukascopy.com/datafeed/EURUSD/2025/02/05/10h_ticks.bi5"


def fetcher(handler: object) -> HttpHourFetcher:
    return HttpHourFetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    )


class TestAnAnswerIsAnAnswer:
    async def test_a_body_comes_back_whole(self) -> None:
        payload = b"\x5d\x00\x00\x80\x00" + bytes(range(64))
        got = await fetcher(lambda _r: httpx.Response(200, content=payload)).fetch(URL)

        assert got == payload

    async def test_404_is_an_empty_hour_and_not_a_failure(self) -> None:
        """True of every weekend and holiday hour. Treating it as a failure would leave
        the queue permanently unable to drain, since the hour never becomes fetchable."""
        got = await fetcher(lambda _r: httpx.Response(404)).fetch(URL)

        assert got is None

    async def test_an_empty_200_is_returned_as_empty_bytes(self) -> None:
        """Distinct from 404: the feed answered and the answer was nothing. The .bi5
        reader is what refuses a payload that is not an LZMA stream, because that is
        where the evidence is."""
        got = await fetcher(lambda _r: httpx.Response(200, content=b"")).fetch(URL)

        assert got == b""


class TestAFailureIsNeverAnEmptyHour:
    @pytest.mark.parametrize("status", [301, 400, 403, 429, 500, 502, 503])
    async def test_every_other_status_raises(self, status: int) -> None:
        """The one thing this class must never do is return empty bytes for a request
        that did not succeed."""
        with pytest.raises(VenueResponseError, match=f"HTTP {status}"):
            await fetcher(lambda _r: httpx.Response(status)).fetch(URL)

    async def test_the_refusal_names_the_url(self) -> None:
        with pytest.raises(VenueResponseError, match=r"10h_ticks\.bi5"):
            await fetcher(lambda _r: httpx.Response(500)).fetch(URL)

    async def test_a_timeout_raises_as_connectivity(self) -> None:
        """Raising is what makes it retried. Returning None would record the hour as
        holding nothing and it would never be fetched again."""

        def timeout(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=None)

        with pytest.raises(VenueConnectivityError, match="ReadTimeout"):
            await fetcher(timeout).fetch(URL)

    async def test_a_connection_error_raises_as_connectivity(self) -> None:
        def refused(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=None)

        with pytest.raises(VenueConnectivityError, match="ConnectError"):
            await fetcher(refused).fetch(URL)

    async def test_the_connectivity_error_names_the_venue(self) -> None:
        def refused(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=None)

        with pytest.raises(VenueConnectivityError, match="dukascopy"):
            await fetcher(refused).fetch(URL)
