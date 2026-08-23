"""Tests for reading the ingest counters out.

The defect these exist against is not a wrong number. It is that `StreamStats` and
`RecorderStats` were incremented from the day they were written and read by nothing, so
the counters existed and the evidence did not, and a decision in `docs/DECISIONS.md` was
recorded against a dataset the system discarded. A test that only checked arithmetic would
have passed throughout that period.

So these check two things in equal measure: that the arithmetic is right, and that the
values reach somewhere a person can read them, which for metrics means appearing in the
rendered exposition and for the rest means being in the payload the activity logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from tradingsys.app.statsreport import StatsReporter
from tradingsys.observability.metrics import Metrics

WRITE_AT = datetime(2026, 8, 23, 10, 30, tzinfo=UTC)


@dataclass
class FakeStreamStats:
    connections: int = 0
    quotes: int = 0
    resyncs: int = 0
    rejected_messages: int = 0
    silence_timeouts: int = 0
    failures: int = 0
    last_error: str | None = None
    reconnect_delays: list[float] = field(default_factory=list)


@dataclass
class FakeRecorderStats:
    received: int = 0
    written: int = 0
    flushes: int = 0
    write_failures: int = 0
    unknown_instruments: dict[str, int] = field(default_factory=dict)
    last_write_at: datetime | None = None

    @property
    def buffered(self) -> int:
        return self.received - self.written


@pytest.fixture
def metrics() -> Metrics:
    return Metrics.create(service="tradingsys", environment="test", version="0")


@pytest.fixture
def stream() -> FakeStreamStats:
    return FakeStreamStats()


@pytest.fixture
def recorder() -> FakeRecorderStats:
    return FakeRecorderStats()


@pytest.fixture
def reporter(
    metrics: Metrics, stream: FakeStreamStats, recorder: FakeRecorderStats
) -> StatsReporter:
    return StatsReporter(
        stream=stream,
        recorder=recorder,
        metrics=metrics,
        venue="bybit",
        stream_name="orderbook.1",
        source="bybit",
    )


def counter(metrics: Metrics, name: str, **labels: str) -> float:
    value = metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


class TestDeltas:
    def test_the_first_report_publishes_everything_seen_so_far(
        self, reporter: StatsReporter, metrics: Metrics, stream: FakeStreamStats
    ) -> None:
        stream.quotes = 120
        reporter.report()
        assert (
            counter(
                metrics,
                "tradingsys_venue_stream_events_total",
                venue="bybit",
                stream="orderbook.1",
            )
            == 120
        )

    def test_later_reports_publish_only_what_changed(
        self, reporter: StatsReporter, metrics: Metrics, stream: FakeStreamStats
    ) -> None:
        stream.quotes = 120
        reporter.report()
        stream.quotes = 200
        reporter.report()
        assert (
            counter(
                metrics,
                "tradingsys_venue_stream_events_total",
                venue="bybit",
                stream="orderbook.1",
            )
            == 200
        ), "a counter incremented by the total each time would report 320"

    def test_a_quiet_window_publishes_nothing_and_does_not_fail(
        self, reporter: StatsReporter, metrics: Metrics, stream: FakeStreamStats
    ) -> None:
        stream.quotes = 10
        reporter.report()
        reporter.report()
        assert (
            counter(
                metrics,
                "tradingsys_venue_stream_events_total",
                venue="bybit",
                stream="orderbook.1",
            )
            == 10
        )

    def test_a_counter_that_goes_backwards_is_clamped(
        self, reporter: StatsReporter, stream: FakeStreamStats
    ) -> None:
        # It cannot happen while the process lives, and if it ever does, the client
        # library refuses a negative increment and would take the whole report down with
        # it, which would lose the readings that are still good.
        stream.quotes = 100
        reporter.report()
        stream.quotes = 5
        reporter.report()


class TestReconnectsAreDerived:
    def test_the_first_connection_is_not_a_reconnect(
        self, reporter: StatsReporter, metrics: Metrics, stream: FakeStreamStats
    ) -> None:
        stream.connections = 1
        reporter.report()
        assert (
            counter(
                metrics,
                "tradingsys_venue_stream_reconnects_total",
                venue="bybit",
                stream="orderbook.1",
            )
            == 0
        )

    def test_every_connection_after_the_first_is(
        self, reporter: StatsReporter, metrics: Metrics, stream: FakeStreamStats
    ) -> None:
        stream.connections = 4
        reporter.report()
        assert (
            counter(
                metrics,
                "tradingsys_venue_stream_reconnects_total",
                venue="bybit",
                stream="orderbook.1",
            )
            == 3
        )

    def test_a_stream_that_never_connected_reports_none(
        self, reporter: StatsReporter, metrics: Metrics
    ) -> None:
        reporter.report()
        assert (
            counter(
                metrics,
                "tradingsys_venue_stream_reconnects_total",
                venue="bybit",
                stream="orderbook.1",
            )
            == 0
        )


class TestIncidents:
    @pytest.mark.parametrize(
        ("attribute", "kind"),
        [
            ("resyncs", "resync"),
            ("silence_timeouts", "silence_timeout"),
            ("rejected_messages", "rejected_message"),
            ("failures", "connection_failure"),
        ],
    )
    def test_each_kind_is_published_separately(
        self,
        reporter: StatsReporter,
        metrics: Metrics,
        stream: FakeStreamStats,
        attribute: str,
        kind: str,
    ) -> None:
        # Kept apart because the remedies differ: a silence timeout is the venue going
        # quiet, a rejected message is us disagreeing with its payload, and a connection
        # failure is the transport. One counter for all three would say nothing.
        setattr(stream, attribute, 2)
        reporter.report()
        assert (
            counter(
                metrics,
                "tradingsys_venue_stream_incidents_total",
                venue="bybit",
                stream="orderbook.1",
                kind=kind,
            )
            == 2
        )


class TestTheRecorder:
    def test_received_and_written_are_separate_outcomes(
        self, reporter: StatsReporter, metrics: Metrics, recorder: FakeRecorderStats
    ) -> None:
        # The question this exists to answer is how many quotes arrived against how many
        # reached storage. One number cannot answer it.
        recorder.received = 500
        recorder.written = 480
        reporter.report()
        assert (
            counter(metrics, "tradingsys_recorder_quotes_total", source="bybit", outcome="received")
            == 500
        )
        assert (
            counter(metrics, "tradingsys_recorder_quotes_total", source="bybit", outcome="written")
            == 480
        )

    def test_buffered_quotes_are_a_gauge_of_what_would_be_lost(
        self, reporter: StatsReporter, metrics: Metrics, recorder: FakeRecorderStats
    ) -> None:
        recorder.received = 500
        recorder.written = 480
        reporter.report()
        assert counter(metrics, "tradingsys_recorder_buffered_quotes", source="bybit") == 20

    def test_a_failed_flush_is_its_own_outcome(
        self, reporter: StatsReporter, metrics: Metrics, recorder: FakeRecorderStats
    ) -> None:
        recorder.flushes = 9
        recorder.write_failures = 1
        reporter.report()
        assert (
            counter(metrics, "tradingsys_recorder_flushes_total", source="bybit", outcome="ok") == 9
        )
        assert (
            counter(metrics, "tradingsys_recorder_flushes_total", source="bybit", outcome="failed")
            == 1
        )

    def test_unknown_instrument_quotes_are_counted_without_the_symbol(
        self, reporter: StatsReporter, metrics: Metrics, recorder: FakeRecorderStats
    ) -> None:
        # An unknown symbol is by definition an unexpected value, and unexpected values
        # as label values are unbounded cardinality. The total is safe; the symbols go to
        # the log.
        recorder.unknown_instruments = {"SOLUSDT": 3, "XRPUSDT": 1}
        payload = reporter.report()
        assert (
            counter(metrics, "tradingsys_recorder_unknown_instrument_quotes_total", source="bybit")
            == 4
        )
        assert payload["unknown_instruments"] == {"SOLUSDT": 3, "XRPUSDT": 1}

    def test_the_last_write_is_an_instant_rather_than_an_age(
        self, reporter: StatsReporter, metrics: Metrics, recorder: FakeRecorderStats
    ) -> None:
        # An age computed here would be as stale as the exporter. An instant lets the age
        # be computed at query time, so a stopped exporter shows an age that keeps
        # growing rather than one frozen at whatever it last published.
        recorder.last_write_at = WRITE_AT
        reporter.report()
        assert (
            counter(metrics, "tradingsys_recorder_last_write_timestamp_seconds", source="bybit")
            == WRITE_AT.timestamp()
        )

    def test_nothing_written_yet_leaves_the_instant_unset(
        self, reporter: StatsReporter, metrics: Metrics
    ) -> None:
        reporter.report()
        assert (
            metrics.registry.get_sample_value(
                "tradingsys_recorder_last_write_timestamp_seconds", {"source": "bybit"}
            )
            is None
        ), "publishing zero would place the last write at 1970 and read as extreme staleness"


class TestThePayloadCarriesWhatMetricsCannot:
    def test_the_last_error_string_is_in_the_payload(
        self, reporter: StatsReporter, stream: FakeStreamStats
    ) -> None:
        # The reconnect loop swallows the exception by design, so this string is the only
        # place the reason survives, and it cannot be a label.
        stream.last_error = "ConnectionClosedError: 1006"
        assert reporter.report()["last_error"] == "ConnectionClosedError: 1006"

    def test_the_most_recent_reconnect_delay_is_reported(
        self, reporter: StatsReporter, stream: FakeStreamStats
    ) -> None:
        stream.reconnect_delays = [0.4, 1.9]
        assert reporter.report()["last_reconnect_delay"] == 1.9

    def test_totals_are_reported_beside_the_deltas_metrics_get(
        self, reporter: StatsReporter, stream: FakeStreamStats, recorder: FakeRecorderStats
    ) -> None:
        # The log line is the copy that survives Prometheus being down or its retention
        # passing, so it carries absolute values rather than the window.
        stream.quotes = 10
        recorder.written = 8
        reporter.report()
        stream.quotes = 25
        recorder.written = 20
        payload = reporter.report()
        assert payload["quotes"] == 25
        assert payload["written"] == 20


class TestTheSeriesAreExposed:
    """A metric that is not in the exposition is not evidence, whatever it holds."""

    def test_every_new_series_appears_in_the_rendered_output(
        self, reporter: StatsReporter, metrics: Metrics, stream: FakeStreamStats
    ) -> None:
        stream.connections = 2
        stream.quotes = 5
        stream.resyncs = 1
        reporter.report()
        rendered = metrics.render().decode()
        for series in (
            "tradingsys_venue_stream_events_total",
            "tradingsys_venue_stream_reconnects_total",
            "tradingsys_venue_stream_incidents_total",
            "tradingsys_recorder_quotes_total",
            "tradingsys_recorder_buffered_quotes",
        ):
            assert series in rendered
