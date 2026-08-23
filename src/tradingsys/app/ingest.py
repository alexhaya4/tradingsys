"""The ingest process: the supervised assembly that the 72 hour run needs.

`SPEC.md` section 3.1 names ingest as one of the five processes, collecting market data
and writing it to the store. This is that process, assembled from the components built
during phase 2 and supervised on progress rather than on liveness.

**What it runs.** A registry sync so instrument definitions come from the venues and
their drift is reported, a crypto quote stream into the tick recorder, a periodic
Dukascopy backfill over the hours the gap detector finds missing, and a counter export so
that what the stream and the recorder saw is readable from outside the process. Each is a
supervised activity with its own progress deadline, so one stalling is visible as itself
rather than as a quiet reduction in throughput.

**What it does not run yet, and why that is stated rather than hidden.** There is no
forex quote stream. cTrader spot subscription is a protocol path this client does not
speak yet, so the forex leg contributes history through the backfill and nothing live.
A 72 hour continuous ingestion run against both venues cannot be claimed until that
exists, and the exit criteria in `SPEC.md` section 8 say both venues.

**Progress deadlines are sized against what the work does**, not chosen for symmetry.
The crypto stream is continuous and should report within seconds. The backfill runs on
an interval and may legitimately find nothing to do, so it reports progress each time it
completes a pass rather than each time it writes a row, and its deadline is a multiple of
its interval.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, final

from tradingsys.app.supervisor import ProgressCheck, SupervisedActivity, Supervisor
from tradingsys.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from tradingsys.app.gapcheck import GapMonitor
    from tradingsys.app.statsreport import StatsReporter
    from tradingsys.config.settings import IngestSettings
    from tradingsys.core.clock import Clock
    from tradingsys.core.instrument import InstrumentId
    from tradingsys.marketdata.backfill_job import BackfillJob
    from tradingsys.marketdata.recorder import QuoteRecorder
    from tradingsys.marketdata.registry import InstrumentSource, RegistrySync
    from tradingsys.venues.models import Quote

__all__ = ["IngestPlan", "IngestProcess"]

logger = get_logger("app.ingest")


@final
@dataclass(frozen=True, slots=True)
class IngestPlan:
    """Intervals and deadlines for the ingest activities.

    Attributes:
        registry_interval: How often instrument definitions are refreshed from venues.
            Venue metadata changes rarely, so this is measured in hours, but it is not
            once at startup: a broker that changes a minimum lot mid-run must be noticed
            during the run rather than at the next restart.
        backfill_interval: How often the backfill looks for missing hours.
        backfill_window: How far back the backfill considers. Required rather than
            defaulted; how much history to hold is a decision with a storage cost.
        quote_deadline: Longest the crypto stream may go without a quote before it is
            stalled. Bybit publishes a level one snapshot at least every three seconds
            even when nothing changes, so silence well past that is death rather than a
            quiet market.
        registry_deadline: Longest between completed registry syncs.
        backfill_deadline: Longest between completed backfill passes.
        stats_interval: How often the stream and recorder counters are published.
        stats_deadline: Longest between completed publications.
    """

    registry_interval: timedelta
    backfill_interval: timedelta
    backfill_window: timedelta
    quote_deadline: timedelta
    registry_deadline: timedelta
    backfill_deadline: timedelta
    gap_interval: timedelta
    gap_deadline: timedelta
    stats_interval: timedelta
    stats_deadline: timedelta

    @classmethod
    def from_settings(cls, settings: IngestSettings) -> IngestPlan:
        """Build the plan from configuration.

        The relationships between these values are validated on
        :class:`~tradingsys.config.settings.IngestSettings`, not here, so a bad
        combination fails at startup with the other configuration errors rather than
        when the first backfill pass silently skips an hour.
        """
        return cls(
            registry_interval=timedelta(seconds=settings.registry_interval_seconds),
            backfill_interval=timedelta(seconds=settings.backfill_interval_seconds),
            backfill_window=timedelta(seconds=settings.backfill_window_seconds),
            quote_deadline=timedelta(seconds=settings.quote_deadline_seconds),
            registry_deadline=timedelta(seconds=settings.registry_deadline_seconds),
            backfill_deadline=timedelta(seconds=settings.backfill_deadline_seconds),
            gap_interval=timedelta(seconds=settings.gap_interval_seconds),
            gap_deadline=timedelta(seconds=settings.gap_deadline_seconds),
            stats_interval=timedelta(seconds=settings.stats_interval_seconds),
            stats_deadline=timedelta(seconds=settings.stats_deadline_seconds),
        )


@final
class IngestProcess:
    """Assembles and supervises the ingest activities."""

    __slots__ = (
        "_backfill",
        "_clock",
        "_gaps",
        "_plan",
        "_quotes",
        "_recorder",
        "_registry",
        "_row_ids",
        "_sources",
        "_stats",
        "_supervisor",
    )

    def __init__(
        self,
        plan: IngestPlan,
        clock: Clock,
        *,
        registry: RegistrySync,
        sources: Sequence[InstrumentSource],
        recorder: QuoteRecorder,
        quotes: Callable[[], AsyncIterator[Quote]],
        backfill: Sequence[BackfillJob] = (),
        gaps: GapMonitor | None = None,
        stats: StatsReporter | None = None,
        row_ids: Mapping[InstrumentId, int] | None = None,
    ) -> None:
        self._plan = plan
        self._clock = clock
        self._registry = registry
        self._sources = tuple(sources)
        self._recorder = recorder
        self._quotes = quotes
        self._backfill = tuple(backfill)
        self._gaps = gaps
        self._stats = stats
        self._row_ids = dict(row_ids or {})
        self._supervisor = Supervisor(clock)

    @property
    def supervisor(self) -> Supervisor:
        return self._supervisor

    def progress_check(self) -> ProgressCheck:
        """The readiness check to register on the application's health registry."""
        return ProgressCheck(self._supervisor)

    def register_activities(self) -> None:
        """Declare every activity. Separate from starting them so a test can inspect."""
        self._supervisor.register(
            SupervisedActivity(
                name="crypto_quotes",
                run=self._run_quotes,
                progress_deadline=self._plan.quote_deadline,
                restart_backoff=1.0,
                max_restart_backoff=60.0,
            )
        )
        self._supervisor.register(
            SupervisedActivity(
                name="instrument_registry",
                run=self._run_registry,
                progress_deadline=self._plan.registry_deadline,
                restart_backoff=5.0,
                max_restart_backoff=300.0,
            )
        )
        if self._backfill:
            self._supervisor.register(
                SupervisedActivity(
                    name="dukascopy_backfill",
                    run=self._run_backfill,
                    progress_deadline=self._plan.backfill_deadline,
                    restart_backoff=5.0,
                    max_restart_backoff=300.0,
                )
            )
        if self._gaps is not None:
            self._supervisor.register(
                SupervisedActivity(
                    name="gap_detection",
                    run=self._run_gap_detection,
                    progress_deadline=self._plan.gap_deadline,
                    restart_backoff=5.0,
                    max_restart_backoff=300.0,
                )
            )
        if self._stats is not None:
            self._supervisor.register(
                SupervisedActivity(
                    name="stats_export",
                    run=self._run_stats,
                    progress_deadline=self._plan.stats_deadline,
                    restart_backoff=5.0,
                    max_restart_backoff=300.0,
                )
            )

    def start(self) -> None:
        self._supervisor.start()
        logger.info(
            "ingest started",
            activities=sorted(self._supervisor.states),
            forex_live_quotes=False,
        )

    async def stop(self) -> None:
        """Stop supervision, then flush whatever the recorder still holds.

        The flush comes after the stream is cancelled and is not optional: a batch that
        was accepted and not yet written is data this system claimed to have recorded.
        """
        await self._supervisor.stop()
        written = await self._recorder.flush()
        logger.info("ingest stopped", flushed=written)

    # ------------------------------------------------------------------
    # activities
    # ------------------------------------------------------------------

    async def _run_quotes(self, progress: Callable[[], None]) -> None:
        """Record the crypto quote stream, reporting progress per quote."""

        async def reporting() -> AsyncIterator[Quote]:
            async for quote in self._quotes():
                progress()
                yield quote

        await self._recorder.run(reporting())

    async def _run_registry(self, progress: Callable[[], None]) -> None:
        """Refresh instrument definitions on an interval, forever."""
        while True:
            for source in self._sources:
                report = await self._registry.sync(source)
                if report.moved:
                    logger.warning("venue metadata moved during the run", detail=report.summary())
            progress()
            await asyncio.sleep(self._plan.registry_interval.total_seconds())

    async def _run_backfill(self, progress: Callable[[], None]) -> None:
        """Queue and drain missing hours on an interval, forever.

        Progress is reported per completed pass rather than per row, because a pass that
        finds nothing missing is the steady state and is not a stall.
        """
        while True:
            now = self._clock.now().replace(minute=0, second=0, microsecond=0)
            window_start = now - self._plan.backfill_window
            for job in self._backfill:
                report = await job.run(window_start=window_start, window_end=now)
                if report.stats.failed:
                    logger.warning(
                        "backfill hours failed and will be retried",
                        failed=report.stats.failed,
                        detail=report.summary(),
                    )
            progress()
            await asyncio.sleep(self._plan.backfill_interval.total_seconds())

    async def _run_stats(self, progress: Callable[[], None]) -> None:
        """Publish the stream and recorder counters on an interval, forever.

        Supervised like everything else, because an exporter that dies silently returns
        the system to the state this activity was written to end: counters incremented
        and read by nobody. It is deliberately the last activity registered, so a failure
        here is visibly about observation rather than about ingestion.
        """
        if self._stats is None:  # pragma: no cover - registration guards this
            return
        while True:
            logger.info("ingest counters", **self._stats.report())
            progress()
            await asyncio.sleep(self._plan.stats_interval.total_seconds())

    async def _run_gap_detection(self, progress: Callable[[], None]) -> None:
        """Look for gaps on an interval, forever.

        Progress is reported per completed pass rather than per gap, because finding
        nothing is the steady state and is not a stall. A pass that finds gaps has done
        its job exactly as much as one that does not.
        """
        if self._gaps is None:  # pragma: no cover - registration guards this
            return
        while True:
            found = await self._gaps.run_once(self._row_ids)
            if found:
                logger.info(
                    "gap detection pass found gaps",
                    gaps=len(found),
                    repairable=self._gaps.stats.repairable_found,
                    permanent=self._gaps.stats.permanent_found,
                )
            progress()
            await asyncio.sleep(self._plan.gap_interval.total_seconds())
