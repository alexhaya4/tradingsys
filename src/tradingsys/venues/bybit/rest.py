"""Bybit v5 public market data over HTTP.

Only the public surface: instrument metadata, tickers, and server time. Nothing here
signs a request, so nothing here can place an order, which is a property worth keeping
while the only thing running continuously is a recorder.

Three behaviours are the reason this is a module rather than a few calls to httpx.

**The envelope is not the status code.** Bybit answers almost everything with HTTP 200
and puts the real outcome in ``retCode``. Code that checks ``response.raise_for_status``
and parses ``result`` will happily read ``{"retCode": 10001, "result": {}}`` as an empty
instrument list, and an empty list of instruments is indistinguishable from a venue
that has delisted everything. Every response goes through one envelope check that
raises on a non-zero code.

**Being blocked costs more than being slow.** The published ceiling is 600 requests per
five seconds per IP, and exceeding it earns an HTTP 403 and a ten minute block, not a
retryable error. Requests therefore pass through a token bucket configured far below
the ceiling, and a 403 is surfaced as a rate limit failure with that ten minutes
spelled out, because the natural reaction to seeing 403 is to retry, which extends it.

**Retries are for the failures that retrying can fix.** Timeouts, connection drops, and
5xx are retried with exponential backoff and jitter. A rejected request is not: the
same malformed parameter will be just as malformed on the fourth attempt, and retrying
it only burns rate budget that the recorder needs.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self, final

import httpx

from tradingsys.core.clock import ensure_utc
from tradingsys.venues.bybit.instruments import VENUE
from tradingsys.venues.errors import (
    VenueConnectivityError,
    VenueRateLimitError,
    VenueResponseError,
)
from tradingsys.venues.ratelimit import RateLimiter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

type _Sleep = Callable[[float], Awaitable[None]]

__all__ = [
    "BLOCK_SECONDS_ON_403",
    "RETRYABLE_STATUS",
    "BybitRestClient",
]

BLOCK_SECONDS_ON_403: Final = 600.0
"""How long Bybit blocks an IP that exceeds the request ceiling, per its documentation."""

RETRYABLE_STATUS: Final = frozenset({500, 502, 503, 504})
"""Server side failures worth a second attempt. 403 is absent on purpose: it means the
IP is already blocked, and retrying prolongs the block."""

_RATE_LIMIT_CODES: Final = frozenset({10006, 10018})
"""``retCode`` values Bybit uses for "too many visits" and IP restriction."""

_MAX_PAGES: Final = 100
"""Pagination guard. Instrument lists run to a few hundred rows at a page size of 1000,
so a hundred pages means the cursor is not advancing and the loop would never end."""


@final
class BybitRestClient:
    """Public market data from one Bybit environment.

    The base URL is supplied by configuration and is never chosen here, so a testnet
    deployment cannot reach mainnet by taking a different code path.
    """

    __slots__ = (
        "_backoff",
        "_base_url",
        "_client",
        "_jitter",
        "_limiter",
        "_max_retries",
        "_sleep",
    )

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient,
        limiter: RateLimiter,
        max_retries: int = 5,
        backoff_seconds: float = 0.5,
        jitter: Callable[[float], float] | None = None,
        sleep: _Sleep | None = None,
    ) -> None:
        """
        Args:
            base_url: REST root, for example ``https://api.bybit.com``.
            client: An httpx client, owned by the caller so that connection pooling and
                proxy settings are decided once for the process.
            limiter: Token bucket sized from configuration.
            max_retries: Additional attempts after the first for retryable failures.
            backoff_seconds: Base delay, doubled per attempt.
            jitter: Maps a delay to a jittered delay. Injected so tests are
                deterministic; defaults to full jitter, uniform in ``[0, delay]``,
                which is what stops a fleet of reconnecting clients from synchronising
                into a thundering herd.
            sleep: Awaitable delay used between retries. Injected so the retry schedule
                can be asserted without the suite actually waiting for it.
        """
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._limiter = limiter
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._jitter: Callable[[float], float] = jitter or (
            # Jitter, not cryptography: predictability here costs nothing.
            lambda delay: random.uniform(0, delay)
        )
        self._sleep: _Sleep = sleep or asyncio.sleep

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # endpoints
    # ------------------------------------------------------------------

    async def instruments(
        self, category: str, *, symbol: str | None = None, limit: int = 1000
    ) -> tuple[Mapping[str, Any], ...]:
        """Every instrument in ``category``, following the cursor to the last page.

        Args:
            category: ``spot`` or ``linear``. Passed through as the venue spells it.
            symbol: Restrict to one instrument. Omitted returns the whole category.
            limit: Page size, capped by the venue at 1000.

        Raises:
            VenueResponseError: The envelope reports a failure, or the pages do not end.
        """
        params: dict[str, str | int] = {"category": category, "limit": limit}
        if symbol is not None:
            params["symbol"] = symbol
        rows: list[Mapping[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page = dict(params) if cursor is None else dict(params) | {"cursor": cursor}
            result = await self._get("/v5/market/instruments-info", page)
            rows.extend(_rows(result))
            cursor = result.get("nextPageCursor") or None
            if cursor is None:
                return tuple(rows)
        raise VenueResponseError(
            VENUE,
            f"instruments-info for {category} did not stop paginating after {_MAX_PAGES} "
            f"pages; the cursor is not advancing",
        )

    async def tickers(
        self, category: str, *, symbol: str | None = None
    ) -> tuple[Mapping[str, Any], ...]:
        """Ticker snapshots, which carry the funding time a perpetual needs."""
        params: dict[str, str | int] = {"category": category}
        if symbol is not None:
            params["symbol"] = symbol
        return tuple(_rows(await self._get("/v5/market/tickers", params)))

    async def server_time(self) -> datetime:
        """The venue's clock.

        Worth having because every timestamp we store is ours until proven otherwise: a
        host whose clock has drifted writes ticks that are ordered wrongly against every
        other source, and nothing about the data itself reveals it.
        """
        result = await self._get("/v5/market/time", {})
        raw = result.get("timeNano") or result.get("timeSecond")
        if not isinstance(raw, str) or not raw.isdigit():
            raise VenueResponseError(VENUE, f"server time is missing or not numeric: {raw!r}")
        nanoseconds = int(raw) if result.get("timeNano") else int(raw) * 1_000_000_000
        # Integer arithmetic, not nanoseconds / 1e9: a float loses the low digits of a
        # nanosecond epoch outright. datetime holds microseconds, so the remainder is
        # truncated rather than rounded, which keeps the value at or before the instant
        # the venue reported instead of a few hundred nanoseconds after it.
        whole_seconds, remainder = divmod(nanoseconds, 1_000_000_000)
        return ensure_utc(
            datetime.fromtimestamp(whole_seconds, tz=UTC)
            + timedelta(microseconds=remainder // 1000)
        )

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        attempt = 0
        while True:
            await self._limiter.acquire()
            try:
                response = await self._client.get(f"{self._base_url}{path}", params=dict(params))
            except httpx.TimeoutException as error:
                await self._sleep_before_retry(attempt, path, f"timed out: {error}")
                attempt += 1
                continue
            except httpx.TransportError as error:
                await self._sleep_before_retry(attempt, path, f"transport failure: {error}")
                attempt += 1
                continue

            if response.status_code == httpx.codes.FORBIDDEN:
                # Not retried, and not reported as a generic failure: the IP is blocked
                # for ten minutes and every further request extends nothing but the
                # confusion of whoever reads the logs.
                raise VenueRateLimitError(
                    VENUE,
                    f"{path} returned 403, which Bybit uses for exceeding the request "
                    f"ceiling of 600 requests per five seconds per IP. The block lasts "
                    f"about ten minutes and retrying does not shorten it.",
                    retry_after_seconds=BLOCK_SECONDS_ON_403,
                )
            if response.status_code in RETRYABLE_STATUS:
                await self._sleep_before_retry(attempt, path, f"HTTP {response.status_code}")
                attempt += 1
                continue
            if response.status_code != httpx.codes.OK:
                raise VenueResponseError(
                    VENUE, f"{path} returned HTTP {response.status_code}: {response.text[:200]}"
                )
            return _envelope(path, response)

    async def _sleep_before_retry(self, attempt: int, path: str, reason: str) -> None:
        if attempt >= self._max_retries:
            raise VenueConnectivityError(
                VENUE, f"{path} failed after {attempt + 1} attempts, last {reason}"
            )
        await self._sleep(self._jitter(self._backoff * (2**attempt)))


def _envelope(path: str, response: httpx.Response) -> Mapping[str, Any]:
    """Unwrap ``result``, refusing anything the venue did not report as success."""
    try:
        payload = response.json()
    except ValueError as error:
        raise VenueResponseError(
            VENUE, f"{path} returned a body that is not JSON: {response.text[:200]}"
        ) from error
    if not isinstance(payload, dict):
        raise VenueResponseError(VENUE, f"{path} returned {type(payload).__name__}, not an object")
    code = payload.get("retCode")
    if code != 0:
        message = payload.get("retMsg", "")
        if code in _RATE_LIMIT_CODES:
            raise VenueRateLimitError(VENUE, f"{path} was rate limited: {code} {message}")
        raise VenueResponseError(VENUE, f"{path} failed: retCode {code}, retMsg {message!r}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise VenueResponseError(
            VENUE, f"{path} reported success but carried no result object: {payload!r}"
        )
    return result


def _rows(result: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    rows = result.get("list")
    if not isinstance(rows, list):
        raise VenueResponseError(VENUE, f"expected a list in the result, got {type(rows).__name__}")
    for row in rows:
        if not isinstance(row, dict):
            raise VenueResponseError(VENUE, f"expected objects in the list, got {row!r}")
    return rows
