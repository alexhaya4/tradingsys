"""Building the ingest process from configuration and connected dependencies.

This is the root every live ingest component terminated at while it did not exist.
`docs/DECISIONS.md` records why that mattered: a component constructed by nothing is
finished and unreachable, and three of them were marked complete before anyone traced
the graph.

**It is a function rather than a class because it does one thing once.** Assembly needs
the database connected, since instrument row ids come from it, so it cannot happen in
`Application.build`, which does no I/O by design.

**What is assembled and what is not.** The crypto leg is complete: metadata, stream,
recorder, registry sync. The forex leg contributes history through the backfill and no
live quotes, because a forex stream without reconnection would die on its first
disconnection and a leg known to be broken is worse than an absent one. The spot
subscription exists and waits for reconnection with resynchronisation.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
import websockets

from tradingsys.app.gapcheck import GapMonitor
from tradingsys.app.ingest import IngestPlan, IngestProcess
from tradingsys.core.errors import TradingSysError
from tradingsys.core.instrument import InstrumentId
from tradingsys.core.provenance import TickSource
from tradingsys.marketdata.backfill import BackfillPlan, HttpHourFetcher
from tradingsys.marketdata.backfill_job import BackfillJob
from tradingsys.marketdata.recorder import QuoteRecorder
from tradingsys.marketdata.registry import RegistrySync
from tradingsys.observability.logging import get_logger
from tradingsys.persistence.backfill import BackfillRepository
from tradingsys.venues.bybit.rest import BybitRestClient
from tradingsys.venues.bybit.source import BybitInstrumentSource
from tradingsys.venues.bybit.stream import BybitPublicStream
from tradingsys.venues.errors import InstrumentNotFoundError
from tradingsys.venues.ratelimit import RateLimiter

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tradingsys.config.settings import Settings
    from tradingsys.core.clock import Clock
    from tradingsys.core.currency import CurrencyRegistry
    from tradingsys.core.instrument import Instrument
    from tradingsys.marketdata.registry import InstrumentSource
    from tradingsys.persistence.audit import AuditLog
    from tradingsys.persistence.database import Database
    from tradingsys.persistence.repositories import InstrumentRepository, MarketDataRepository
    from tradingsys.venues.bybit.stream import Connect

__all__ = ["AssembledIngest", "assemble_ingest"]

logger = get_logger("app.assembly")

CRYPTO_VENUE = "bybit"

CRYPTO_CATEGORY = "linear"
"""Product category appended to the configured stream root.

`docs/DECISIONS.md` records the crypto product as linear perpetuals rather than spot,
and the venue puts the category in the path, so one configured root serves every
category rather than needing a field per product."""


class AssembledIngest:
    """The ingest process and the resources it owns, so shutdown can release them."""

    __slots__ = ("_closers", "process")

    def __init__(self, process: IngestProcess, closers: Sequence[object]) -> None:
        self.process = process
        self._closers = tuple(closers)

    async def aclose(self) -> None:
        """Close every owned resource, attempting each even if another fails."""
        for closer in self._closers:
            close = getattr(closer, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                logger.exception(
                    "failed to close an ingest resource", resource=type(closer).__name__
                )


async def assemble_ingest(
    settings: Settings,
    clock: Clock,
    *,
    database: Database,
    instruments: InstrumentRepository,
    market_data: MarketDataRepository,
    audit_log: AuditLog,
    currencies: CurrencyRegistry,
    connect: Connect | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> AssembledIngest:
    """Build the ingest process for this deployment.

    Requires a connected database: instrument row ids are read from it, and a recorder
    without them would count every quote as an unknown instrument and write nothing.

    Args:
        connect: Opens the crypto stream's socket. Defaults to a real connection. It is
            injectable for the same reason `BybitPublicStream` already takes it: the
            wiring from socket to stored row is the half of the risk that construction
            alone does not cover, and exercising it against a scripted peer keeps that
            test off the network and out of CI's dependencies.

    Raises:
        InstrumentNotFoundError: An instrument in the configured universe has never been
            stored. The registry sync populates the table, so this means the process is
            starting before its first sync has ever run, which is a deployment ordering
            problem rather than a venue one.
    """
    universe = settings.universe
    crypto = settings.venues.crypto.get(CRYPTO_VENUE)
    if crypto is None or not crypto.enabled:
        raise TradingSysError(
            f"the {CRYPTO_VENUE} venue is absent or disabled, so this process would "
            f"start, report healthy, and record nothing"
        )

    rest = BybitRestClient(
        crypto.rest_url,
        client=http_client
        if http_client is not None
        else httpx.AsyncClient(timeout=crypto.request_timeout_seconds),
        limiter=RateLimiter(crypto.max_requests_per_second),
        max_retries=crypto.max_retries,
        backoff_seconds=crypto.retry_backoff_seconds,
    )
    sources: list[InstrumentSource] = [
        BybitInstrumentSource(rest, universe.venue_symbols(CRYPTO_VENUE), currencies)
    ]

    registry = RegistrySync(instruments, audit=audit_log)
    await _seed_instruments(registry, sources)

    # Row ids for everything recorded live. Read once: they are surrogate keys and do
    # not change, and looking them up per quote would put a query in the hot path.
    row_ids: dict[InstrumentId, int] = {}
    stored: dict[InstrumentId, Instrument] = {}
    symbols: dict[str, InstrumentId] = {}
    for ref in universe.for_venue(CRYPTO_VENUE):
        instrument = await _require(instruments, ref.venue, ref.symbol)
        row_ids[instrument.id] = await instruments.row_id(instrument.id)
        stored[instrument.id] = instrument
        symbols[ref.venue_symbol] = instrument.id

    recorder = QuoteRecorder(
        repository=market_data,
        row_ids=row_ids,
        source=TickSource.BYBIT,
        batch_size=settings.ingest.recorder_batch_size,
        flush_interval_seconds=settings.ingest.recorder_flush_interval_seconds,
    )

    stream = BybitPublicStream(
        f"{crypto.ws_public_url}/{CRYPTO_CATEGORY}",
        symbols,
        connect=connect if connect is not None else websockets.connect,
        ping_interval_seconds=crypto.ws_ping_interval_seconds,
        receive_timeout_seconds=crypto.ws_receive_timeout_seconds,
    )

    backfill_jobs = await _forex_backfill(
        settings, database=database, instruments=instruments, market_data=market_data
    )

    monitor = GapMonitor(
        market_data=market_data,
        queue=BackfillRepository(database),
        universe=universe,
        instruments=stored,
        sources=dict.fromkeys(stored, TickSource.BYBIT),
        audit=audit_log,
        clock=clock,
        settings=settings.ingest,
    )

    process = IngestProcess(
        IngestPlan.from_settings(settings.ingest),
        clock,
        registry=registry,
        sources=sources,
        recorder=recorder,
        quotes=stream.quotes,
        backfill=backfill_jobs,
        gaps=monitor,
        row_ids=row_ids,
    )
    logger.info(
        "ingest assembled",
        crypto_instruments=len(row_ids),
        backfill_jobs=len(backfill_jobs),
        forex_live_quotes=False,
    )
    return AssembledIngest(process, [rest])


async def _forex_backfill(
    settings: Settings,
    *,
    database: Database,
    instruments: InstrumentRepository,
    market_data: MarketDataRepository,
) -> tuple[BackfillJob, ...]:
    """One job per instrument that configuration says has a historical feed.

    An instrument with no historical source gets no job, which is the crypto case and is
    the same fact that makes a crypto gap permanent.
    """
    with_history = settings.universe.with_history()
    if not with_history:
        return ()

    queue = BackfillRepository(database)
    fetcher = HttpHourFetcher(
        httpx.AsyncClient(timeout=settings.ingest.backfill_fetch_timeout_seconds)
    )
    jobs: list[BackfillJob] = []
    for ref in with_history:
        instrument = await _require(instruments, ref.venue, ref.symbol)
        # Both guaranteed by InstrumentRef validation: a source without a symbol cannot
        # be fetched and a symbol without a source has nothing to fetch it from.
        assert ref.historical_source is not None
        assert ref.historical_symbol is not None
        plan = BackfillPlan(
            source=ref.historical_source,
            instrument_id=instrument.id,
            instrument_row_id=await instruments.row_id(instrument.id),
            venue_symbol=ref.historical_symbol,
            digits=instrument.price_precision,
            concurrency=settings.ingest.backfill_concurrency,
            max_attempts_per_hour=settings.ingest.backfill_max_attempts_per_hour,
            backoff_seconds=settings.ingest.backfill_backoff_seconds,
            stale_claim_after=timedelta(seconds=settings.ingest.backfill_stale_claim_seconds),
        )
        jobs.append(BackfillJob(plan, instrument, queue=queue, fetcher=fetcher, store=market_data))
    return tuple(jobs)


async def _require(instruments: InstrumentRepository, venue: str, symbol: str) -> Instrument:
    """The stored definition for a configured instrument, or a loud failure.

    The registry sync is what populates the table, so an absence here means the process
    is starting before its first sync has ever run. That is a deployment ordering
    problem, and starting anyway would record quotes against no instrument and count
    every one as unknown.
    """
    instrument = await instruments.get(InstrumentId(venue=venue, symbol=symbol))
    if instrument is None:
        raise InstrumentNotFoundError(venue, symbol)
    return instrument


async def _seed_instruments(registry: RegistrySync, sources: Sequence[InstrumentSource]) -> None:
    """Refresh instrument definitions before anything looks one up.

    **This ordering is load bearing and its absence was a startup deadlock.** The row id
    lookup below needs stored definitions, the registry sync is what stores them, and
    the sync used to run only as a supervised activity started after assembly had
    already succeeded. On a fresh database assembly therefore failed, the process
    exited, and the sync that would have fixed it never ran. It could not recover by
    restarting, because every restart met the same empty table.

    **A sync that fails is tolerated here and a missing instrument is not.** The two are
    different failures. A venue that is briefly unreachable should not stop a process
    that already holds definitions from an earlier run, since it can record the moment
    the venue returns. A configured instrument that resolves to nothing afterwards is
    fatal, and the caller raises on it, because recording quotes against an instrument
    this system has no definition for is how every one of them becomes an unknown
    instrument counter rather than a row.
    """
    for source in sources:
        try:
            report = await registry.sync(source)
        except Exception as exc:
            # Deliberately broad: any failure to reach a venue is the same decision
            # here, and the specific type matters to the log rather than to the branch.
            logger.warning(
                "initial registry sync failed, falling back to stored definitions",
                venue=source.venue,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            logger.info(
                "initial registry sync complete",
                venue=source.venue,
                summary=report.summary(),
            )
