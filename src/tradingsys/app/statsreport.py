"""Reading the ingest counters out, because a counter nobody reads is not evidence.

`StreamStats` and `RecorderStats` were maintained from the day they were written and read
by nothing: no log line, no metric, no health output. They reset on every restart. That
made a claim in `docs/DECISIONS.md` false, since the region decision was recorded against
a dataset the 72 hour run was said to produce and in fact discarded, and it is the same
defect class as the components that were complete and unreached. A counter that is
incremented and never read is a component whose only consumer does not exist.

**Two consumers, and they fail differently.** Prometheus holds the series over time and is
what a question like "how often did this stream reconnect last week" is actually answered
from. The log line holds what does not fit a metric, principally the last error string and
which symbols arrived for instruments the registry does not know, and it survives
Prometheus being down or the retention window passing.

**Deltas are computed here rather than by the metric.** A Prometheus counter may only be
incremented, and these are absolute values that live as long as the process, so the
reporter keeps what it last saw and increments by the difference. A process restart resets
both sides at once, which is exactly what a counter reset means and what `rate()` is built
to handle.

**Reconnects are derived rather than counted.** The stream increments `connections` when it
opens one, including the first, so reconnects are `connections - 1` and the first
connection is not a reconnect. Deriving it here rather than adding a second counter to the
stream keeps one definition of what happened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, final

from tradingsys.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from tradingsys.observability.metrics import Metrics

__all__ = ["RecorderStatsLike", "StatsReporter", "StreamStatsLike"]

logger = get_logger("app.statsreport")


class StreamStatsLike(Protocol):
    """What this reporter reads from a stream's counters.

    A protocol rather than the concrete `StreamStats`, so that the forex stream feeds the
    same reporter when it exists without the app layer importing one venue's module to
    describe a shape both venues have.
    """

    @property
    def connections(self) -> int: ...
    @property
    def quotes(self) -> int: ...
    @property
    def resyncs(self) -> int: ...
    @property
    def rejected_messages(self) -> int: ...
    @property
    def silence_timeouts(self) -> int: ...
    @property
    def failures(self) -> int: ...
    @property
    def last_error(self) -> str | None: ...
    @property
    def reconnect_delays(self) -> list[float]: ...


class RecorderStatsLike(Protocol):
    """What this reporter reads from the recorder's counters."""

    @property
    def received(self) -> int: ...
    @property
    def written(self) -> int: ...
    @property
    def flushes(self) -> int: ...
    @property
    def write_failures(self) -> int: ...
    @property
    def unknown_instruments(self) -> dict[str, int]: ...
    @property
    def last_write_at(self) -> datetime | None: ...
    @property
    def buffered(self) -> int: ...


@final
class StatsReporter:
    """Publishes stream and recorder counters to metrics and to the log."""

    __slots__ = (
        "_metrics",
        "_previous",
        "_recorder",
        "_source",
        "_stream",
        "_stream_name",
        "_venue",
    )

    def __init__(
        self,
        *,
        stream: StreamStatsLike,
        recorder: RecorderStatsLike,
        metrics: Metrics,
        venue: str,
        stream_name: str,
        source: str,
    ) -> None:
        self._stream = stream
        self._recorder = recorder
        self._metrics = metrics
        self._venue = venue
        self._stream_name = stream_name
        self._source = source
        self._previous: dict[str, int] = {}

    def _delta(self, name: str, current: int) -> int:
        """How much this counter moved since the last observation.

        Clamped at zero. A counter cannot legitimately go backwards while the process
        lives, and if one ever does, publishing a negative increment is refused by the
        client library and would take the whole report down with it.
        """
        previous = self._previous.get(name, 0)
        self._previous[name] = current
        return max(0, current - previous)

    def report(self) -> Mapping[str, object]:
        """Publish one observation window and return what was observed.

        The return value is what the caller logs. Returning it rather than logging here
        keeps the decision about log level and cadence with the activity that owns the
        loop.
        """
        stream = self._stream
        recorder = self._recorder

        # connections counts every connection including the first, and the first is not a
        # reconnect. Derived from one counter rather than counted twice.
        reconnects_total = max(0, stream.connections - 1)
        incidents = {
            "resync": self._delta("resyncs", stream.resyncs),
            "silence_timeout": self._delta("silence_timeouts", stream.silence_timeouts),
            "rejected_message": self._delta("rejected_messages", stream.rejected_messages),
            "connection_failure": self._delta("failures", stream.failures),
        }
        self._metrics.record_stream(
            self._venue,
            self._stream_name,
            quotes=self._delta("quotes", stream.quotes),
            reconnects=self._delta("reconnects", reconnects_total),
            incidents=incidents,
        )

        unknown_total = sum(recorder.unknown_instruments.values())
        last_write = recorder.last_write_at
        self._metrics.record_recorder(
            self._source,
            received=self._delta("received", recorder.received),
            written=self._delta("written", recorder.written),
            flushes=self._delta("flushes", recorder.flushes),
            write_failures=self._delta("write_failures", recorder.write_failures),
            unknown_instrument_quotes=self._delta("unknown_instruments", unknown_total),
            buffered=recorder.buffered,
            last_write_epoch=None if last_write is None else last_write.timestamp(),
        )

        delays = stream.reconnect_delays
        return {
            "venue": self._venue,
            "stream": self._stream_name,
            "connections": stream.connections,
            "reconnects": reconnects_total,
            "quotes": stream.quotes,
            "resyncs": stream.resyncs,
            "silence_timeouts": stream.silence_timeouts,
            "rejected_messages": stream.rejected_messages,
            "stream_failures": stream.failures,
            "last_error": stream.last_error,
            "last_reconnect_delay": delays[-1] if delays else None,
            "received": recorder.received,
            "written": recorder.written,
            "buffered": recorder.buffered,
            "flushes": recorder.flushes,
            "write_failures": recorder.write_failures,
            "unknown_instruments": dict(recorder.unknown_instruments),
            "last_write_at": None if last_write is None else last_write.isoformat(),
        }
