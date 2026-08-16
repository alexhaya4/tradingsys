"""Tests for the Bybit public REST client.

Requests are served by an httpx MockTransport rather than by the network, so the whole
file runs offline and deterministically, and the bodies are the recorded responses in
``data/`` wherever a realistic one matters.

The cases that earn their place are the ones where Bybit's behaviour differs from what
an HTTP client would assume: a failure delivered inside a 200, a 403 that means "you
are blocked for ten minutes" rather than "try again", and a paginated list whose first
page looks complete.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from tradingsys.venues.bybit.rest import BLOCK_SECONDS_ON_403, BybitRestClient
from tradingsys.venues.errors import (
    VenueConnectivityError,
    VenueRateLimitError,
    VenueResponseError,
)
from tradingsys.venues.ratelimit import RateLimiter

DATA = Path(__file__).parent / "data"
BASE = "https://api-testnet.bybit.com"


def recorded_body(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((DATA / name).read_text())
    return body


def ok(result: dict[str, Any]) -> dict[str, Any]:
    return {"retCode": 0, "retMsg": "OK", "result": result}


class Recorder:
    """Captures the requests a test provokes, and answers them from a script."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def client(
    handler: Recorder, *, max_retries: int = 5, sleeps: list[float] | None = None
) -> BybitRestClient:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return BybitRestClient(
        BASE,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        limiter=RateLimiter(1000),
        max_retries=max_retries,
        backoff_seconds=0.5,
        # No randomness: the schedule is asserted, so it has to be the schedule and not
        # a sample from it.
        jitter=lambda delay: delay,
        sleep=sleep,
    )


class TestInstruments:
    async def test_the_recorded_response_comes_back_as_rows(self) -> None:
        handler = Recorder(httpx.Response(200, json=recorded_body("instruments_spot_BTCUSDT.json")))
        async with client(handler) as api:
            rows = await api.instruments("spot", symbol="BTCUSDT")
        assert [row["symbol"] for row in rows] == ["BTCUSDT"]

    async def test_the_category_and_symbol_reach_the_query(self) -> None:
        handler = Recorder(
            httpx.Response(200, json=recorded_body("instruments_linear_ETHUSDT.json"))
        )
        async with client(handler) as api:
            await api.instruments("linear", symbol="ETHUSDT")
        query = handler.requests[0].url.params
        assert query["category"] == "linear"
        assert query["symbol"] == "ETHUSDT"
        assert handler.requests[0].url.path == "/v5/market/instruments-info"

    async def test_pagination_follows_the_cursor_to_the_end(self) -> None:
        # A first page that looks complete is exactly what a naive client stops at,
        # and the missing instruments are then absent from the registry with no error.
        handler = Recorder(
            httpx.Response(200, json=ok({"list": [{"symbol": "A"}], "nextPageCursor": "page-2"})),
            httpx.Response(200, json=ok({"list": [{"symbol": "B"}], "nextPageCursor": ""})),
        )
        async with client(handler) as api:
            rows = await api.instruments("linear")
        assert [row["symbol"] for row in rows] == ["A", "B"]
        assert handler.requests[1].url.params["cursor"] == "page-2"

    async def test_a_cursor_that_never_advances_is_refused(self) -> None:
        # Otherwise the loop runs until the process is killed, holding the rate budget.
        handler = Recorder(
            httpx.Response(200, json=ok({"list": [{"symbol": "A"}], "nextPageCursor": "same"}))
        )
        async with client(handler) as api:
            with pytest.raises(VenueResponseError, match="not advancing"):
                await api.instruments("linear")


class TestTheEnvelopeIsNotTheStatusCode:
    async def test_a_failure_inside_a_200_is_raised(self) -> None:
        # The failure this client exists for. Without the check, result is absent and
        # the caller reads zero instruments, which is a valid looking answer.
        handler = Recorder(
            httpx.Response(200, json={"retCode": 10001, "retMsg": "params error", "result": {}})
        )
        async with client(handler) as api:
            with pytest.raises(VenueResponseError, match="retCode 10001"):
                await api.instruments("spot")

    @pytest.mark.parametrize("code", [10006, 10018])
    async def test_a_rate_limit_code_is_a_rate_limit_error(self, code: int) -> None:
        handler = Recorder(
            httpx.Response(200, json={"retCode": code, "retMsg": "Too many visits!", "result": {}})
        )
        async with client(handler) as api:
            with pytest.raises(VenueRateLimitError):
                await api.instruments("spot")

    async def test_success_with_no_result_object_is_refused(self) -> None:
        handler = Recorder(httpx.Response(200, json={"retCode": 0, "retMsg": "OK"}))
        async with client(handler) as api:
            with pytest.raises(VenueResponseError, match="carried no result"):
                await api.instruments("spot")

    async def test_a_body_that_is_not_json_is_refused(self) -> None:
        handler = Recorder(httpx.Response(200, text="<html>gateway</html>"))
        async with client(handler) as api:
            with pytest.raises(VenueResponseError, match="not JSON"):
                await api.instruments("spot")

    async def test_a_json_array_body_is_refused(self) -> None:
        # Valid JSON, wrong shape. Indexing into it would raise somewhere further in
        # with a message about integers and strings.
        handler = Recorder(httpx.Response(200, json=[{"retCode": 0}]))
        async with client(handler) as api:
            with pytest.raises(VenueResponseError, match="returned list, not an object"):
                await api.instruments("spot")

    async def test_a_result_without_a_list_is_refused(self) -> None:
        handler = Recorder(httpx.Response(200, json=ok({"category": "spot"})))
        async with client(handler) as api:
            with pytest.raises(VenueResponseError, match="expected a list"):
                await api.instruments("spot")

    async def test_a_list_of_non_objects_is_refused(self) -> None:
        handler = Recorder(httpx.Response(200, json=ok({"list": ["BTCUSDT"]})))
        async with client(handler) as api:
            with pytest.raises(VenueResponseError, match="expected objects"):
                await api.instruments("spot")


class TestBeingBlockedIsNotBeingThrottled:
    async def test_a_403_is_reported_as_a_rate_limit_with_the_block_length(self) -> None:
        handler = Recorder(httpx.Response(403, text="access too frequent"))
        async with client(handler) as api:
            with pytest.raises(VenueRateLimitError) as raised:
                await api.instruments("spot")
        assert raised.value.retry_after_seconds == BLOCK_SECONDS_ON_403
        assert "ten minutes" in str(raised.value)

    async def test_a_403_is_not_retried(self) -> None:
        # Retrying a block does not shorten it, and every attempt spends budget that
        # the recorder needs when the block lifts.
        handler = Recorder(httpx.Response(403, text="access too frequent"))
        async with client(handler) as api:
            with pytest.raises(VenueRateLimitError):
                await api.instruments("spot")
        assert len(handler.requests) == 1


class TestRetries:
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_a_server_error_is_retried_and_then_succeeds(self, status: int) -> None:
        handler = Recorder(
            httpx.Response(status, text="upstream"),
            httpx.Response(200, json=ok({"list": [{"symbol": "BTCUSDT"}]})),
        )
        async with client(handler) as api:
            rows = await api.instruments("spot")
        assert len(rows) == 1
        assert len(handler.requests) == 2

    async def test_the_backoff_doubles(self) -> None:
        sleeps: list[float] = []
        handler = Recorder(
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json=ok({"list": []})),
        )
        async with client(handler, sleeps=sleeps) as api:
            await api.instruments("spot")
        assert sleeps == [0.5, 1.0, 2.0]

    async def test_it_gives_up_after_the_configured_attempts(self) -> None:
        handler = Recorder(httpx.Response(503, text="upstream"))
        async with client(handler, max_retries=2) as api:
            with pytest.raises(VenueConnectivityError, match="after 3 attempts"):
                await api.instruments("spot")
        assert len(handler.requests) == 3

    async def test_a_timeout_is_retried(self) -> None:
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(200, json=ok({"list": []}))

        api = BybitRestClient(
            BASE,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            limiter=RateLimiter(1000),
            jitter=lambda delay: delay,
            sleep=_no_sleep,
        )
        async with api:
            assert await api.instruments("spot") == ()
        assert attempts["count"] == 2

    async def test_a_connection_failure_is_retried(self) -> None:
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json=ok({"list": []}))

        api = BybitRestClient(
            BASE,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            limiter=RateLimiter(1000),
            jitter=lambda delay: delay,
            sleep=_no_sleep,
        )
        async with api:
            await api.instruments("spot")
        assert attempts["count"] == 2

    async def test_a_client_error_is_not_retried(self) -> None:
        # A 404 is the same 404 on the fourth attempt.
        handler = Recorder(httpx.Response(404, text="no such path"))
        async with client(handler) as api:
            with pytest.raises(VenueResponseError, match="HTTP 404"):
                await api.instruments("spot")
        assert len(handler.requests) == 1


class TestTickersAndTime:
    async def test_the_funding_time_survives_the_round_trip(self) -> None:
        handler = Recorder(httpx.Response(200, json=recorded_body("tickers_linear_BTCUSDT.json")))
        async with client(handler) as api:
            rows = await api.tickers("linear", symbol="BTCUSDT")
        assert rows[0]["nextFundingTime"] == "1786896000000"

    async def test_server_time_is_read_from_nanoseconds(self) -> None:
        handler = Recorder(
            httpx.Response(
                200,
                json=ok({"timeSecond": "1786896000", "timeNano": "1786896000123456789"}),
            )
        )
        async with client(handler) as api:
            assert await api.server_time() == datetime(2026, 8, 16, 16, 0, 0, 123456, tzinfo=UTC)

    async def test_server_time_falls_back_to_seconds(self) -> None:
        handler = Recorder(httpx.Response(200, json=ok({"timeSecond": "1786896000"})))
        async with client(handler) as api:
            assert await api.server_time() == datetime(2026, 8, 16, 16, tzinfo=UTC)

    async def test_a_missing_server_time_is_refused(self) -> None:
        handler = Recorder(httpx.Response(200, json=ok({})))
        async with client(handler) as api:
            with pytest.raises(VenueResponseError, match="server time is missing"):
                await api.server_time()


class TestRateLimiting:
    async def test_every_request_passes_through_the_limiter(self) -> None:
        # The guard against a burst of parallel fetches earning a ten minute block,
        # which is how the Dukascopy sizing run failed.
        waits: list[float] = []
        now = 0.0

        async def sleep(seconds: float) -> None:
            nonlocal now
            waits.append(seconds)
            now += seconds

        handler = Recorder(httpx.Response(200, json=ok({"list": []})))
        api = BybitRestClient(
            BASE,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            # The limiter's own clock is faked too, so the assertion is about the
            # schedule it produces rather than about how fast this machine ran.
            limiter=RateLimiter(2, capacity=1, monotonic=lambda: now, sleep=sleep),
            jitter=lambda delay: delay,
            sleep=_no_sleep,
        )
        async with api:
            for _ in range(3):
                await api.tickers("linear")
        assert waits == [pytest.approx(0.5), pytest.approx(0.5)]


async def _no_sleep(seconds: float) -> None:
    """Retry delays are asserted elsewhere; here they only need not to happen."""
