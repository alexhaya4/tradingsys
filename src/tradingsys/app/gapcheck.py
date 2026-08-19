"""Detecting gaps in recorded data, and doing something about them.

`SPEC.md` section 8 makes gap detection proven by deliberate disconnection an exit
criterion for phase 2. The capability existed as a tested library that no running code
called, which `docs/DECISIONS.md` records: `find_gaps` had no production caller at all,
so there was nothing to prove. This is the caller.

**What happens to a detected gap depends on whether anything can repair it, and that is
configuration rather than a venue name.** `InstrumentRef.historical_source` decides it.
An instrument with a feed has its missing hours queued for backfill. An instrument
without one has the gap recorded as permanent, because Bybit publishes no historical
quote data at all and crypto spread history begins when the recorder starts.

**Gaps do not fail readiness, and that is deliberate.** Readiness answers whether this
process should be given work. A gap from last Tuesday does not change that answer, and a
permanent crypto gap would pin readiness red forever, which teaches an operator to
ignore the one signal that matters. The gap that should fail readiness is the one
happening now, and `ProgressCheck` already reports exactly that: an activity past its
progress deadline while its task is alive and its failure count is zero. The split is by
tense. Ongoing failure fails readiness; historical fact is recorded.

**Which provenance counts as coverage is supplied, not inferred.** An instrument can
hold ticks from more than one source: a forex pair records live from the broker and
backfills from Dukascopy, and those are different series in the same table. This monitor
is told which source to measure rather than deriving it from the venue name, because
coercing a venue string into a provenance enum fails on any venue not in it and, worse,
would silently measure one series while the gap was in the other. **Only one source per
instrument is examined today**, which is correct while the only live stream is crypto,
and is an open question the moment forex records live and backfills into the same table.

**Detection that only logs is barely better than none**, so every gap lands in three
places: a counter, an audit entry with a correlation ID, and for repairable gaps a row
in the backfill queue. The queue is also what distinguishes a gap being repaired from
one that is not, since a queued hour carries its own claim and attempt count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, final

from tradingsys.core.provenance import TickSource
from tradingsys.marketdata.gaps import find_gaps
from tradingsys.observability.correlation import correlation_id
from tradingsys.observability.logging import get_logger
from tradingsys.persistence.audit import AuditCategory

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from tradingsys.config.settings import IngestSettings, UniverseSettings
    from tradingsys.core.clock import Clock
    from tradingsys.core.instrument import Instrument, InstrumentId
    from tradingsys.marketdata.gaps import Gap
    from tradingsys.persistence.audit import AuditLog
    from tradingsys.persistence.backfill import BackfillRepository
    from tradingsys.persistence.repositories import MarketDataRepository

__all__ = ["GapMonitor", "GapStats", "hours_covering"]

logger = get_logger("app.gapcheck")


@final
@dataclass(slots=True)
class GapStats:
    """What the monitor has found, kept per outcome rather than as one total.

    Repairable and permanent are counted apart because they call for different actions:
    one is a queue to watch draining, the other is a fact about the data that every
    later backtest has to be told.
    """

    passes: int = 0
    repairable_found: int = 0
    permanent_found: int = 0
    hours_queued: int = 0
    seconds_missing: float = 0.0
    per_instrument: dict[str, int] = field(default_factory=dict)


@final
class GapMonitor:
    """Finds gaps in what was recorded, queues what can be repaired, records the rest."""

    __slots__ = (
        "_audit",
        "_clock",
        "_instruments",
        "_market_data",
        "_queue",
        "_settings",
        "_sources",
        "_stats",
        "_universe",
    )

    def __init__(
        self,
        *,
        market_data: MarketDataRepository,
        queue: BackfillRepository,
        universe: UniverseSettings,
        instruments: Mapping[InstrumentId, Instrument],
        sources: Mapping[InstrumentId, TickSource],
        audit: AuditLog,
        clock: Clock,
        settings: IngestSettings,
    ) -> None:
        self._market_data = market_data
        self._queue = queue
        self._universe = universe
        self._instruments = dict(instruments)
        self._sources = dict(sources)
        self._audit = audit
        self._clock = clock
        self._settings = settings
        self._stats = GapStats()

    @property
    def stats(self) -> GapStats:
        return self._stats

    async def run_once(self, row_ids: Mapping[InstrumentId, int]) -> tuple[Gap, ...]:
        """Examine the recent window for every instrument and act on what it finds.

        The window ends one interval short of now, not at now, because the most recent
        seconds are indistinguishable from a quiet market until enough time has passed
        to tell them apart. Reporting them would make every pass find a gap at its own
        trailing edge.

        Returns:
            Every gap found this pass, across all instruments, for the caller to log or
            assert on.
        """
        now = self._clock.now()
        window_end = now - timedelta(seconds=self._settings.gap_settle_seconds)
        window_start = window_end - timedelta(seconds=self._settings.gap_window_seconds)
        found: list[Gap] = []

        for instrument_id, row_id in row_ids.items():
            instrument = self._instruments.get(instrument_id)
            source = self._sources.get(instrument_id)
            if instrument is None or source is None:
                continue
            gaps = await self._for_instrument(instrument, source, row_id, window_start, window_end)
            found.extend(gaps)

        self._stats.passes += 1
        return tuple(found)

    async def _for_instrument(
        self,
        instrument: Instrument,
        source: TickSource,
        row_id: int,
        start: datetime,
        end: datetime,
    ) -> tuple[Gap, ...]:
        covered = await self._market_data.tick_coverage(
            row_id,
            source,
            start=start,
            end=end,
            max_quiet=timedelta(seconds=self._settings.gap_max_quiet_seconds),
        )
        gaps = find_gaps(
            instrument.schedule,
            covered,
            start=start,
            end=end,
            minimum=timedelta(seconds=self._settings.gap_minimum_seconds),
        )
        if not gaps:
            return ()

        ref = next(
            (r for r in self._universe.instruments if r.symbol == instrument.id.symbol), None
        )
        repairable = ref is not None and ref.historical_source is not None
        key = str(instrument.id)
        self._stats.per_instrument[key] = self._stats.per_instrument.get(key, 0) + len(gaps)
        self._stats.seconds_missing += sum((g.end - g.start).total_seconds() for g in gaps)

        if repairable and ref is not None and ref.historical_source is not None:
            self._stats.repairable_found += len(gaps)
            hours = sorted({hour for gap in gaps for hour in hours_covering(gap)})
            queued = await self._queue.enqueue(ref.historical_source, row_id, hours)
            self._stats.hours_queued += queued
            await self._record(instrument, gaps, repairable=True, queued=queued)
            logger.info(
                "gaps found and queued for backfill",
                instrument=key,
                gaps=len(gaps),
                hours_queued=queued,
            )
        else:
            self._stats.permanent_found += len(gaps)
            await self._record(instrument, gaps, repairable=False, queued=0)
            # Warning rather than info: this is data that cannot be recovered, and the
            # only remedy is to know it is missing when the backtest is read.
            logger.warning(
                "gaps found that cannot be repaired, no historical feed for this venue",
                instrument=key,
                gaps=len(gaps),
                seconds_missing=sum((g.end - g.start).total_seconds() for g in gaps),
            )
        return gaps

    async def _record(
        self, instrument: Instrument, gaps: tuple[Gap, ...], *, repairable: bool, queued: int
    ) -> None:
        """Write the finding to the audit log, which is where it survives a restart."""
        with correlation_id() as trace:
            await self._audit.append(
                correlation_id=trace,
                category=AuditCategory.MARKET_DATA,
                actor="app.gapcheck",
                action="gaps_detected",
                summary=(
                    f"{len(gaps)} gap(s) in {instrument.id}, "
                    f"{'queued for backfill' if repairable else 'permanent, no historical feed'}"
                ),
                payload={
                    "repairable": repairable,
                    "hours_queued": queued,
                    "gaps": [
                        {"start": gap.start.isoformat(), "end": gap.end.isoformat()} for gap in gaps
                    ],
                },
                instrument_id=instrument.id,
            )


def hours_covering(gap: Gap) -> tuple[datetime, ...]:
    """Every hour start the gap touches, since the archive is fetched by whole hours.

    A gap of one minute still needs its hour fetched, and a gap that straddles a
    boundary needs both. Truncating to the start hour alone would leave the second half
    of a straddling gap unqueued and therefore never repaired.
    """
    first = gap.start.replace(minute=0, second=0, microsecond=0)
    hours: list[datetime] = []
    hour = first
    while hour < gap.end:
        hours.append(hour)
        hour += timedelta(hours=1)
    return tuple(hours)
