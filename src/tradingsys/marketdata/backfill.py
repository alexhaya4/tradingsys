"""The resumable Dukascopy backfill runner.

The pieces it drives already existed: the `.bi5` reader decodes an hour, the gap
detector says which hours are missing, and `backfill_hours` holds the queue. This is
the loop between them, and its whole job is to be interruptible without losing or
duplicating work.

**An hour ends in exactly one of three states, and none of them is silence.** It is
complete with a row count, which may legitimately be zero for an hour the feed holds
nothing for. It is failed with the reason, and will be claimed again by a later run.
Or the process died mid-hour, in which case the claim goes stale and another run takes
it. There is deliberately no path that leaves an hour neither done nor retryable, and
none that marks an hour complete without having written what it found.

**Rows and status are committed together.** A crash between writing ticks and marking
the hour complete would leave the hour claimable again, which at worst refetches an
hour whose rows are already stored and whose duplicate timestamps the tick table
ignores. The opposite ordering would mark an hour complete with no data in it, and
nothing would ever look for that hour again. Cheap repetition is the correct failure
here; a silent hole is not.

**Concurrency is capped and the cap is the venue's, not ours.** Dukascopy publishes no
documented rate limit and answers aggressive clients with error pages rather than 429s,
which the reader is careful to refuse rather than decode as an empty hour. Three
concurrent fetches is the configured ceiling.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Protocol, final

import httpx

from tradingsys.core.errors import TradingSysError
from tradingsys.core.provenance import TickSource
from tradingsys.marketdata.dukascopy import Bi5DecodeError, decode_hour, hour_url
from tradingsys.observability.logging import get_logger
from tradingsys.venues.errors import VenueConnectivityError, VenueResponseError
from tradingsys.venues.models import Quote

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tradingsys.core.instrument import InstrumentId
    from tradingsys.persistence.backfill import BackfillHour, BackfillRepository

__all__ = [
    "BackfillPlan",
    "BackfillRunner",
    "BackfillStats",
    "HourFetcher",
    "HttpHourFetcher",
]

logger = get_logger("marketdata.backfill")

_OK: Final = 200
_NOT_FOUND: Final = 404


class HourFetcher(Protocol):
    """Fetches the raw bytes of one archived hour.

    Narrow on purpose. The runner needs no HTTP vocabulary, and a test can supply a
    fetcher that fails, returns an error page, or returns a recorded hour, without a
    server or a network.
    """

    async def fetch(self, url: str) -> bytes | None:
        """Return the body, or ``None`` when the feed holds nothing for that hour.

        ``None`` and empty bytes mean the same thing to the caller and both are an
        answer rather than a failure. Anything that could not be fetched must raise, so
        that a transport problem is never recorded as an empty hour.
        """
        ...


@final
class HttpHourFetcher:
    """Fetches archived hours over HTTP.

    The status handling is the whole of it, and each branch is a decision rather than
    a default.

    **404 means the feed holds nothing for that hour**, which is true of every weekend
    hour and every holiday. It is an answer, and it becomes a complete hour with zero
    rows. Treating it as a failure would leave the queue permanently unable to drain.

    **Any other non-success raises**, including the 200 that carries an HTML error page
    under load. That one is not caught here on purpose: it is indistinguishable from a
    real body at this layer, and the `.bi5` reader already refuses a payload that is not
    an LZMA stream rather than decoding it as an empty hour. The check belongs where the
    evidence is.

    **A timeout or a connection failure raises**, so it is retried. The one thing this
    class must never do is return empty bytes for a request that did not succeed,
    because that would record a hole in history that never existed.
    """

    __slots__ = ("_client",)

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def fetch(self, url: str) -> bytes | None:
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise VenueConnectivityError(
                "dukascopy", f"fetching {url} failed: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code == _NOT_FOUND:
            return None
        if response.status_code != _OK:
            raise VenueResponseError(
                "dukascopy",
                f"{url} returned HTTP {response.status_code}. Only 200 and 404 are "
                f"answers here; anything else is retried rather than recorded as an "
                f"hour with no data in it.",
            )
        return response.content


@final
@dataclass(slots=True)
class BackfillStats:
    """What one run did. Counters, because the interesting questions are about rates."""

    claimed: int = 0
    completed: int = 0
    failed: int = 0
    rows_written: int = 0
    empty_hours: int = 0
    """Hours the feed holds nothing for. Every weekend hour is one, so a run over a
    month of history is expected to produce many; a run that produces only these is a
    sign the URL or the symbol is wrong."""
    retries: int = 0
    last_error: str | None = None


@final
@dataclass(frozen=True, slots=True)
class BackfillPlan:
    """What to back fill, and how hard to try.

    Attributes:
        source: Queue key, also stored on every tick as its provenance.
        instrument_id: Canonical instrument, for the quotes that are written.
        instrument_row_id: Database id, for the queue and the tick table.
        venue_symbol: The feed's own symbol, which is not the canonical one.
        digits: Price decimals, needed to scale the archive's integers. Comes from
            venue metadata rather than from a table in this module.
        concurrency: Concurrent fetches. Capped by the venue's tolerance, not ours.
        max_attempts_per_hour: Tries within one run before the hour is left failed for
            a later run to pick up. A failed hour is never abandoned, so this bounds
            the run rather than the retrying.
        backoff_seconds: First backoff. Doubles per attempt, with jitter.
        stale_claim_after: How long an ``in_progress`` claim survives before another
            run may reclaim it.
    """

    source: str
    instrument_id: InstrumentId
    instrument_row_id: int
    venue_symbol: str
    digits: int
    concurrency: int
    max_attempts_per_hour: int
    backoff_seconds: float
    stale_claim_after: timedelta
    base_url: str | None = None

    def __post_init__(self) -> None:
        if self.concurrency <= 0:
            raise TradingSysError(f"concurrency must be positive, got {self.concurrency}")
        if self.max_attempts_per_hour <= 0:
            raise TradingSysError(
                f"max_attempts_per_hour must be positive, got {self.max_attempts_per_hour}"
            )
        if self.backoff_seconds <= 0:
            raise TradingSysError(f"backoff_seconds must be positive, got {self.backoff_seconds}")


class TickStore(Protocol):
    """The write side of the tick table, narrowed to what the runner uses."""

    async def store_ticks(
        self, instrument_row_id: int, source: TickSource, ticks: Sequence[Quote]
    ) -> int: ...


@final
class BackfillRunner:
    """Drains the backfill queue for one instrument and source."""

    __slots__ = ("_fetcher", "_plan", "_queue", "_random", "_store")

    def __init__(
        self,
        plan: BackfillPlan,
        *,
        queue: BackfillRepository,
        fetcher: HourFetcher,
        store: TickStore,
        jitter: random.Random | None = None,
    ) -> None:
        self._plan = plan
        self._queue = queue
        self._fetcher = fetcher
        self._store = store
        # Injected so a test can make the backoff deterministic. Jitter exists to stop
        # three workers retrying in lockstep after a shared failure, which is exactly
        # when a feed is least able to absorb them.
        self._random = jitter if jitter is not None else random.Random()

    async def run(self, *, max_hours: int | None = None) -> BackfillStats:
        """Claim and process hours until the queue is drained or ``max_hours`` is done.

        Args:
            max_hours: Stop after this many hours. ``None`` drains the queue, which is
                what a scheduled backfill wants; a bounded run is for operators.

        Returns:
            What this run did. A run that completes zero hours because the queue was
            empty is a success, not a failure, and reports zeros.
        """
        stats = BackfillStats()
        semaphore = asyncio.Semaphore(self._plan.concurrency)

        while max_hours is None or stats.claimed < max_hours:
            remaining = self._plan.concurrency
            if max_hours is not None:
                remaining = min(remaining, max_hours - stats.claimed)
            if remaining <= 0:
                break

            hours = await self._queue.claim(
                self._plan.source,
                limit=remaining,
                stale_after=self._plan.stale_claim_after,
            )
            if not hours:
                break
            stats.claimed += len(hours)

            await asyncio.gather(
                *(self._process(hour, stats, semaphore) for hour in hours),
                return_exceptions=False,
            )

        logger.info(
            "backfill run finished",
            source=self._plan.source,
            symbol=self._plan.venue_symbol,
            claimed=stats.claimed,
            completed=stats.completed,
            failed=stats.failed,
            rows=stats.rows_written,
            empty_hours=stats.empty_hours,
            retries=stats.retries,
        )
        return stats

    async def _process(
        self, hour: BackfillHour, stats: BackfillStats, semaphore: asyncio.Semaphore
    ) -> None:
        """Take one hour to a terminal state. Never leaves it claimed."""
        async with semaphore:
            try:
                rows = await self._attempt_with_retries(hour, stats)
            except Exception as exc:
                # Nothing here may propagate. An exception escaping this would abandon
                # the other hours in the same gather and leave this one claimed until
                # its claim went stale, which is a slow, silent version of losing it.
                reason = f"{type(exc).__name__}: {exc}"
                stats.failed += 1
                stats.last_error = reason
                logger.warning(
                    "backfill hour failed",
                    source=hour.source,
                    hour=hour.hour_start.isoformat(),
                    attempts=hour.attempts,
                    error=reason,
                )
                await self._queue.mark_failed(hour, error=reason)
                return

            stats.completed += 1
            stats.rows_written += rows
            if rows == 0:
                stats.empty_hours += 1
            await self._queue.mark_complete(hour, rows_written=rows)

    async def _attempt_with_retries(self, hour: BackfillHour, stats: BackfillStats) -> int:
        """Fetch, decode, and store one hour, retrying transport failures.

        A decode failure is not retried. The reader refuses an HTML error page rather
        than decoding it as an empty hour, and refuses a truncated archive, and neither
        becomes valid by asking again in a second. Retrying those would turn a clear
        signal about the feed into a slow one.
        """
        last: Exception | None = None
        for attempt in range(1, self._plan.max_attempts_per_hour + 1):
            try:
                return await self._fetch_decode_store(hour)
            except Bi5DecodeError:
                raise
            except Exception as exc:
                last = exc
                if attempt == self._plan.max_attempts_per_hour:
                    break
                stats.retries += 1
                await asyncio.sleep(self._backoff(attempt))
        assert last is not None
        raise last

    def _backoff(self, attempt: int) -> float:
        """Exponential with full jitter, so concurrent workers do not retry in step."""
        ceiling = self._plan.backoff_seconds * (2 ** (attempt - 1))
        return self._random.uniform(0.0, ceiling)

    async def _fetch_decode_store(self, hour: BackfillHour) -> int:
        url = (
            hour_url(self._plan.venue_symbol, hour.hour_start, base_url=self._plan.base_url)
            if self._plan.base_url is not None
            else hour_url(self._plan.venue_symbol, hour.hour_start)
        )
        payload = await self._fetcher.fetch(url)
        if payload is None:
            payload = b""

        ticks = decode_hour(payload, hour=hour.hour_start, digits=self._plan.digits)
        if not ticks:
            return 0

        quotes = [
            Quote(
                instrument_id=self._plan.instrument_id,
                ts=tick.ts,
                bid=tick.bid,
                ask=tick.ask,
                bid_size=tick.bid_volume,
                ask_size=tick.ask_volume,
            )
            for tick in ticks
        ]
        return await self._store.store_ticks(
            self._plan.instrument_row_id, TickSource.DUKASCOPY, quotes
        )


def hours_between(start: datetime, end: datetime) -> tuple[datetime, ...]:
    """Every hour start in ``[start, end)``, aligned and UTC.

    Raises:
        TradingSysError: The bounds are not aligned to the hour, are naive, or are the
            wrong way round. A range expressed loosely produces a queue that is quietly
            missing its first or last hour.
    """
    for label, value in (("start", start), ("end", end)):
        if value.tzinfo is None:
            raise TradingSysError(f"{label} must be timezone aware, got {value!r}")
        if (value.minute, value.second, value.microsecond) != (0, 0, 0):
            raise TradingSysError(f"{label} must be aligned to the hour, got {value!r}")
    if end < start:
        raise TradingSysError(f"end {end!r} is before start {start!r}")
    span = int((end - start).total_seconds() // 3600)
    return tuple(start + timedelta(hours=index) for index in range(span))
