"""Prometheus metrics.

Metrics are registered on an owned :class:`~prometheus_client.CollectorRegistry` rather
than the library's global one. A global registry makes a second instantiation in the
same process raise a duplicate registration error, which turns every test that touches
metrics into an ordering problem.

Label cardinality is the other thing to get right. Nothing here is labelled by
correlation ID, order ID, or timestamp: those are unbounded, and a metric labelled by
one of them will eventually take the monitoring system down. Instrument is used as a
label only where the set is small and known, and venue and category everywhere else.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

__all__ = ["METRICS_CONTENT_TYPE", "Metrics"]

METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST
"""Content type matching what :meth:`Metrics.render` produces.

It must match the renderer. Declaring the OpenMetrics type while serving the classic
text format makes Prometheus reject every scrape with "data does not end with # EOF",
because an OpenMetrics document requires that trailer and the classic format has none.
"""

_LATENCY_BUCKETS = (
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)
"""Buckets spanning one millisecond to ten seconds.

Chosen for venue round trips and database queries, which is where the interesting
detail sits: the default buckets start at 5ms and lose everything faster than that.
"""


@final
@dataclass(slots=True)
class Metrics:
    """The metrics this process exposes.

    One instance is created at startup and passed to whatever needs it, rather than
    reached for through a module global, so that a test can create its own.
    """

    registry: CollectorRegistry

    build_info: Gauge
    process_start_time: Gauge
    ready: Gauge

    health_checks: Counter
    health_check_duration: Histogram

    venue_requests: Counter
    venue_request_duration: Histogram
    venue_stream_events: Counter
    venue_stream_reconnects: Counter
    venue_stream_incidents: Counter

    recorder_quotes: Counter
    recorder_flushes: Counter
    recorder_unknown_instrument_quotes: Counter
    recorder_buffered_quotes: Gauge
    recorder_last_write_timestamp: Gauge

    db_queries: Counter
    db_query_duration: Histogram
    db_pool_connections: Gauge

    audit_entries: Counter
    decisions: Counter

    @classmethod
    def create(
        cls,
        *,
        service: str,
        environment: str,
        version: str,
        registry: CollectorRegistry | None = None,
    ) -> Self:
        """Build the metric set on a fresh registry.

        Args:
            service: Service name, exposed as a build info label.
            environment: Deployment environment, exposed as a build info label.
            version: Package version, exposed as a build info label.
            registry: Registry to register on. A new one is created when omitted.
        """
        target = CollectorRegistry() if registry is None else registry

        build_info = Gauge(
            "tradingsys_build_info",
            "Build and deployment identity of the running process.",
            ("service", "environment", "version"),
            registry=target,
        )
        build_info.labels(service=service, environment=environment, version=version).set(1)

        process_start_time = Gauge(
            "tradingsys_process_start_time_seconds",
            "Unix timestamp at which the process started.",
            registry=target,
        )
        process_start_time.set(time.time())

        ready = Gauge(
            "tradingsys_ready",
            "1 when every readiness check passed at the last evaluation, 0 otherwise.",
            registry=target,
        )
        ready.set(0)

        return cls(
            registry=target,
            build_info=build_info,
            process_start_time=process_start_time,
            ready=ready,
            health_checks=Counter(
                "tradingsys_health_checks_total",
                "Health check evaluations by check name and outcome.",
                ("check", "status"),
                registry=target,
            ),
            health_check_duration=Histogram(
                "tradingsys_health_check_duration_seconds",
                "Time taken to evaluate one health check.",
                ("check",),
                buckets=_LATENCY_BUCKETS,
                registry=target,
            ),
            venue_requests=Counter(
                "tradingsys_venue_requests_total",
                "Requests sent to a venue, by venue, operation, and outcome.",
                ("venue", "operation", "outcome"),
                registry=target,
            ),
            venue_request_duration=Histogram(
                "tradingsys_venue_request_duration_seconds",
                "Round trip time of a venue request.",
                ("venue", "operation"),
                buckets=_LATENCY_BUCKETS,
                registry=target,
            ),
            venue_stream_events=Counter(
                "tradingsys_venue_stream_events_total",
                "Events received from a venue stream, by venue and stream.",
                ("venue", "stream"),
                registry=target,
            ),
            venue_stream_reconnects=Counter(
                "tradingsys_venue_stream_reconnects_total",
                "Times a venue stream had to reconnect.",
                ("venue", "stream"),
                registry=target,
            ),
            venue_stream_incidents=Counter(
                "tradingsys_venue_stream_incidents_total",
                "Stream events that are not quotes, by kind: resync, silence timeout, "
                "rejected message, connection failure.",
                ("venue", "stream", "kind"),
                registry=target,
            ),
            recorder_quotes=Counter(
                "tradingsys_recorder_quotes_total",
                "Quotes the recorder handled, by source and by whether they reached storage.",
                ("source", "outcome"),
                registry=target,
            ),
            recorder_flushes=Counter(
                "tradingsys_recorder_flushes_total",
                "Recorder flushes to storage, by source and outcome.",
                ("source", "outcome"),
                registry=target,
            ),
            recorder_unknown_instrument_quotes=Counter(
                "tradingsys_recorder_unknown_instrument_quotes_total",
                "Quotes discarded because their instrument is not in the registry. The "
                "symbol is deliberately not a label: an unknown symbol is by definition "
                "an unexpected value, and unexpected values as labels are unbounded "
                "cardinality. The symbols are in the log line.",
                ("source",),
                registry=target,
            ),
            recorder_buffered_quotes=Gauge(
                "tradingsys_recorder_buffered_quotes",
                "Quotes received and not yet written. Data this process claims to have "
                "and would lose if it died now.",
                ("source",),
                registry=target,
            ),
            recorder_last_write_timestamp=Gauge(
                "tradingsys_recorder_last_write_timestamp_seconds",
                "Unix time of the last successful write. Exposed as an instant rather "
                "than an age, so that the age is computed at query time and a stopped "
                "exporter cannot report a freshness it is no longer observing.",
                ("source",),
                registry=target,
            ),
            db_queries=Counter(
                "tradingsys_db_queries_total",
                "Database queries by logical operation and outcome.",
                ("operation", "outcome"),
                registry=target,
            ),
            db_query_duration=Histogram(
                "tradingsys_db_query_duration_seconds",
                "Database query duration by logical operation.",
                ("operation",),
                buckets=_LATENCY_BUCKETS,
                registry=target,
            ),
            db_pool_connections=Gauge(
                "tradingsys_db_pool_connections",
                "Connections in the database pool, by state.",
                ("state",),
                registry=target,
            ),
            audit_entries=Counter(
                "tradingsys_audit_entries_total",
                "Entries written to the audit log, by category.",
                ("category",),
                registry=target,
            ),
            decisions=Counter(
                "tradingsys_decisions_total",
                "Decisions the system made, by category and outcome.",
                ("category", "outcome"),
                registry=target,
            ),
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @contextmanager
    def time_venue_request(self, venue: str, operation: str) -> Iterator[None]:
        """Time a venue request and record its outcome.

        The outcome label distinguishes success from failure, so an error rate can be
        derived without a second counter.
        """
        started = time.perf_counter()
        outcome = "success"
        try:
            yield
        except BaseException:
            outcome = "failure"
            raise
        finally:
            self.venue_request_duration.labels(venue=venue, operation=operation).observe(
                time.perf_counter() - started
            )
            self.venue_requests.labels(venue=venue, operation=operation, outcome=outcome).inc()

    @contextmanager
    def time_query(self, operation: str) -> Iterator[None]:
        """Time a database query and record its outcome."""
        started = time.perf_counter()
        outcome = "success"
        try:
            yield
        except BaseException:
            outcome = "failure"
            raise
        finally:
            self.db_query_duration.labels(operation=operation).observe(
                time.perf_counter() - started
            )
            self.db_queries.labels(operation=operation, outcome=outcome).inc()

    def record_pool(self, stats: dict[str, int]) -> None:
        """Publish current database pool occupancy."""
        for state in ("size", "idle", "in_use", "max_size"):
            if state in stats:
                self.db_pool_connections.labels(state=state).set(stats[state])

    def record_stream(
        self,
        venue: str,
        stream: str,
        *,
        quotes: int,
        reconnects: int,
        incidents: Mapping[str, int],
    ) -> None:
        """Publish one observation window of stream activity.

        Deltas, not totals: these are counters, and the caller owns the arithmetic
        because only it knows what it saw last.
        """
        if quotes:
            self.venue_stream_events.labels(venue=venue, stream=stream).inc(quotes)
        if reconnects:
            self.venue_stream_reconnects.labels(venue=venue, stream=stream).inc(reconnects)
        for kind, count in incidents.items():
            if count:
                self.venue_stream_incidents.labels(venue=venue, stream=stream, kind=kind).inc(count)

    def record_recorder(
        self,
        source: str,
        *,
        received: int,
        written: int,
        flushes: int,
        write_failures: int,
        unknown_instrument_quotes: int,
        buffered: int,
        last_write_epoch: float | None,
    ) -> None:
        """Publish one observation window of recorder activity."""
        if received:
            self.recorder_quotes.labels(source=source, outcome="received").inc(received)
        if written:
            self.recorder_quotes.labels(source=source, outcome="written").inc(written)
        if flushes:
            self.recorder_flushes.labels(source=source, outcome="ok").inc(flushes)
        if write_failures:
            self.recorder_flushes.labels(source=source, outcome="failed").inc(write_failures)
        if unknown_instrument_quotes:
            self.recorder_unknown_instrument_quotes.labels(source=source).inc(
                unknown_instrument_quotes
            )
        self.recorder_buffered_quotes.labels(source=source).set(buffered)
        if last_write_epoch is not None:
            self.recorder_last_write_timestamp.labels(source=source).set(last_write_epoch)

    def record_audit_entry(self, category: str) -> None:
        self.audit_entries.labels(category=category).inc()

    def record_decision(self, category: str, outcome: str) -> None:
        self.decisions.labels(category=category, outcome=outcome).inc()

    def set_ready(self, ready: bool) -> None:
        self.ready.set(1 if ready else 0)

    def render(self) -> bytes:
        """The current metrics in the Prometheus exposition format."""
        return generate_latest(self.registry)
