"""Writing a live quote stream into storage.

The recorder sits between a venue stream and the tick table and does three things that
the stream deliberately does not.

**It batches.** At the observed rates a single instrument produces tens of quotes a
second, and one insert per quote spends the whole budget on round trips. Batches flush
on size or on age, so a busy market flushes on size and a quiet one still lands within
a bounded delay rather than sitting in memory until the next tick.

**It never drops a batch it has accepted.** A write that fails is retried, and the
quotes stay in the buffer until they are written or the recorder gives up loudly.
Quotes discarded here are gone: this venue publishes no historical quote data, so there
is nothing to backfill from and no way to notice afterwards.

**It flushes on the way out.** Cancellation is the normal way this stops, and the
partial batch in hand at that moment is real data. It is written during teardown before
the cancellation is allowed to continue.

Tagging is not optional. Every row is written with the source that produced it, which
is what keeps research data out of the cost model. See
:mod:`tradingsys.core.provenance`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, final

from tradingsys.core.clock import utc_now
from tradingsys.core.errors import DomainError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from datetime import datetime

    from tradingsys.core.instrument import InstrumentId
    from tradingsys.core.provenance import TickSource
    from tradingsys.venues.models import Quote

__all__ = ["QuoteRecorder", "RecorderStats", "TickWriter"]


class TickWriter(Protocol):
    """The one thing the recorder needs from storage.

    Narrower than the repository on purpose: the recorder has no business reading
    ticks, and a protocol this small is satisfied by the real repository without it
    knowing, so nothing has to be adapted in either direction.
    """

    async def store_ticks(
        self, instrument_row_id: int, source: TickSource, ticks: Sequence[Quote]
    ) -> int: ...


@final
@dataclass(slots=True)
class RecorderStats:
    """What was seen, what was written, and what could not be."""

    received: int = 0
    written: int = 0
    flushes: int = 0
    write_failures: int = 0
    unknown_instruments: dict[str, int] = field(default_factory=dict)
    last_write_at: datetime | None = None

    @property
    def buffered(self) -> int:
        """Quotes received but not yet written."""
        return self.received - self.written


@final
class QuoteRecorder:
    """Persists quotes from a stream, batching by size and by age."""

    __slots__ = (
        "_batch_size",
        "_buffers",
        "_flush_interval",
        "_max_retry_backoff",
        "_now",
        "_oldest",
        "_repository",
        "_retry_backoff",
        "_row_ids",
        "_sleep",
        "_source",
        "_stats",
        "_write_attempts",
    )

    def __init__(
        self,
        repository: TickWriter,
        row_ids: Mapping[InstrumentId, int],
        source: TickSource,
        *,
        batch_size: int = 500,
        flush_interval_seconds: float = 5.0,
        write_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
        max_retry_backoff_seconds: float = 30.0,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """
        Args:
            repository: Where ticks are written.
            row_ids: Instrument identity to its database row. Resolved once at start
                rather than per quote, because a lookup per tick at these rates is a
                query per tick.
            source: Provenance tag written on every row.
            batch_size: Quotes per instrument before a flush is forced.
            flush_interval_seconds: Maximum age of the oldest buffered quote. This is
                the bound on how much is in memory when the process dies.
            write_attempts: Attempts per batch before the failure is raised. The quotes
                are retained across attempts.
            retry_backoff_seconds: First delay between write attempts, doubled each
                time. Configuration rather than a constant, because the right value
                depends on whether the database is a socket away or a region away.
            max_retry_backoff_seconds: Ceiling on that doubling.
            now: Clock, injected for tests.
            sleep: Delay between write attempts, injected for tests.

        Raises:
            DomainError: ``batch_size`` or ``write_attempts`` is not positive, or
                ``flush_interval_seconds`` is negative.
        """
        if batch_size <= 0:
            raise DomainError(f"batch_size must be positive, got {batch_size}")
        if write_attempts <= 0:
            raise DomainError(f"write_attempts must be positive, got {write_attempts}")
        if retry_backoff_seconds <= 0:
            raise DomainError(
                f"retry_backoff_seconds must be positive, got {retry_backoff_seconds}"
            )
        if flush_interval_seconds < 0:
            raise DomainError(
                f"flush_interval_seconds must not be negative, got {flush_interval_seconds}"
            )
        self._repository = repository
        self._row_ids = dict(row_ids)
        self._source = source
        self._batch_size = batch_size
        self._flush_interval = flush_interval_seconds
        self._write_attempts = write_attempts
        self._retry_backoff = retry_backoff_seconds
        self._max_retry_backoff = max_retry_backoff_seconds
        self._now: Callable[[], datetime] = now or utc_now
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._buffers: dict[InstrumentId, list[Quote]] = {}
        self._oldest: datetime | None = None
        self._stats = RecorderStats()

    @property
    def stats(self) -> RecorderStats:
        return self._stats

    async def run(self, quotes: AsyncIterator[Quote]) -> None:
        """Consume ``quotes`` until it ends or this task is cancelled.

        Raises:
            asyncio.CancelledError: Re-raised after the buffered quotes are written,
                because a recorder that swallows cancellation cannot be shut down.
        """
        try:
            async for quote in quotes:
                self._stats.received += 1
                buffer = self._buffers.setdefault(quote.instrument_id, [])
                buffer.append(quote)
                now = self._now()
                if self._oldest is None:
                    self._oldest = now
                age = (now - self._oldest).total_seconds()
                if len(buffer) >= self._batch_size or age >= self._flush_interval:
                    await self.flush()
        except asyncio.CancelledError:
            # The batch in hand is real data. Shielded so the write is not cancelled
            # halfway through by the same cancellation that brought us here, and the
            # failure is suppressed because a shutdown that raises over a lost batch
            # tells nobody anything the counters do not already show.
            with contextlib.suppress(Exception):
                await asyncio.shield(self.flush())
            raise
        # A stream that ends on its own is not a shutdown, so a failure to write the
        # last batch surfaces rather than being swallowed.
        await self.flush()

    async def flush(self) -> int:
        """Write every buffered quote. Returns the number of rows written."""
        written = 0
        for instrument_id, buffer in self._buffers.items():
            if not buffer:
                continue
            row_id = self._row_ids.get(instrument_id)
            if row_id is None:
                # Not written, and not silently dropped either: an unregistered
                # instrument is a configuration error, and counting it per symbol is
                # what makes it visible before the storage bill does.
                key = str(instrument_id)
                self._stats.unknown_instruments[key] = self._stats.unknown_instruments.get(
                    key, 0
                ) + len(buffer)
                buffer.clear()
                continue
            written += await self._write(row_id, buffer)
            buffer.clear()
        self._oldest = None
        if written:
            self._stats.flushes += 1
            self._stats.written += written
            self._stats.last_write_at = self._now()
        return written

    async def _write(self, row_id: int, buffer: list[Quote]) -> int:
        attempt = 0
        while True:
            try:
                return await self._repository.store_ticks(row_id, self._source, tuple(buffer))
            except Exception:
                attempt += 1
                self._stats.write_failures += 1
                if attempt >= self._write_attempts:
                    # Raised rather than dropped. Losing quotes quietly is
                    # unrecoverable here, so the recorder stops and says so.
                    raise
                await self._sleep(
                    min(self._retry_backoff * 2 ** (attempt - 1), self._max_retry_backoff)
                )
