"""Tests for the Prometheus metric set."""

from __future__ import annotations

import asyncio

import pytest
from prometheus_client import REGISTRY, CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families

from tradingsys.observability.metrics import METRICS_CONTENT_TYPE, Metrics


def build(registry: CollectorRegistry | None = None) -> Metrics:
    return Metrics.create(
        service="tradingsys", environment="test", version="1.2.3", registry=registry
    )


def sample_value(metrics: Metrics, name: str, **labels: str) -> float | None:
    """Read one sample back out of the rendered exposition."""
    rendered = metrics.render().decode()
    for family in text_string_to_metric_families(rendered):
        for sample in family.samples:
            if sample.name == name and all(
                sample.labels.get(key) == value for key, value in labels.items()
            ):
                return sample.value
    return None


class TestRegistryIsolation:
    def test_two_instances_can_coexist(self) -> None:
        # A global registry would raise a duplicate registration error here, which is
        # exactly the failure that makes metrics untestable.
        first = build()
        second = build()
        assert first.registry is not second.registry

    def test_an_explicit_registry_is_used(self) -> None:
        registry = CollectorRegistry()
        metrics = build(registry)
        assert metrics.registry is registry

    def test_nothing_is_registered_globally(self) -> None:
        build()
        names = {metric.name for metric in REGISTRY.collect()}
        assert "tradingsys_build_info" not in names


class TestStaticMetrics:
    def test_build_info_carries_identity(self) -> None:
        metrics = build()
        value = sample_value(
            metrics,
            "tradingsys_build_info",
            service="tradingsys",
            environment="test",
            version="1.2.3",
        )
        assert value == 1.0

    def test_process_start_time_is_set(self) -> None:
        assert (sample_value(build(), "tradingsys_process_start_time_seconds") or 0) > 0

    def test_ready_starts_at_zero(self) -> None:
        # A process that has not yet evaluated readiness must not report itself ready.
        assert sample_value(build(), "tradingsys_ready") == 0.0

    def test_ready_can_be_set(self) -> None:
        metrics = build()
        metrics.set_ready(True)
        assert sample_value(metrics, "tradingsys_ready") == 1.0
        metrics.set_ready(False)
        assert sample_value(metrics, "tradingsys_ready") == 0.0


class TestVenueTiming:
    def test_a_successful_request_is_counted(self) -> None:
        metrics = build()
        with metrics.time_venue_request("fxbroker", "fetch_candles"):
            pass
        assert (
            sample_value(
                metrics,
                "tradingsys_venue_requests_total",
                venue="fxbroker",
                operation="fetch_candles",
                outcome="success",
            )
            == 1.0
        )

    def test_a_failing_request_is_counted_as_a_failure_and_reraises(self) -> None:
        metrics = build()
        with pytest.raises(RuntimeError), metrics.time_venue_request("fxbroker", "place_order"):
            raise RuntimeError("venue rejected the connection")
        assert (
            sample_value(
                metrics,
                "tradingsys_venue_requests_total",
                venue="fxbroker",
                operation="place_order",
                outcome="failure",
            )
            == 1.0
        )

    def test_a_cancellation_is_recorded_as_a_failure(self) -> None:
        # CancelledError derives from BaseException, so catching only Exception would
        # leave the histogram without an observation during shutdown.
        metrics = build()
        with (
            pytest.raises(asyncio.CancelledError),
            metrics.time_venue_request("fxbroker", "stream"),
        ):
            raise asyncio.CancelledError
        assert (
            sample_value(
                metrics,
                "tradingsys_venue_requests_total",
                venue="fxbroker",
                operation="stream",
                outcome="failure",
            )
            == 1.0
        )

    def test_duration_is_observed(self) -> None:
        metrics = build()
        with metrics.time_venue_request("fxbroker", "fetch_quote"):
            pass
        count = sample_value(
            metrics,
            "tradingsys_venue_request_duration_seconds_count",
            venue="fxbroker",
            operation="fetch_quote",
        )
        assert count == 1.0


class TestQueryTiming:
    def test_success_and_failure(self) -> None:
        metrics = build()
        with metrics.time_query("store_bars"):
            pass
        with pytest.raises(ValueError, match="bad"), metrics.time_query("store_bars"):
            raise ValueError("bad")
        assert (
            sample_value(
                metrics, "tradingsys_db_queries_total", operation="store_bars", outcome="success"
            )
            == 1.0
        )
        assert (
            sample_value(
                metrics, "tradingsys_db_queries_total", operation="store_bars", outcome="failure"
            )
            == 1.0
        )


class TestGaugesAndCounters:
    def test_pool_stats_are_published(self) -> None:
        metrics = build()
        metrics.record_pool({"size": 5, "idle": 2, "in_use": 3, "max_size": 10})
        assert sample_value(metrics, "tradingsys_db_pool_connections", state="in_use") == 3.0
        assert sample_value(metrics, "tradingsys_db_pool_connections", state="max_size") == 10.0

    def test_unknown_pool_keys_are_ignored(self) -> None:
        metrics = build()
        metrics.record_pool({"size": 1, "nonsense": 99})
        assert sample_value(metrics, "tradingsys_db_pool_connections", state="nonsense") is None

    def test_audit_entries_are_counted_by_category(self) -> None:
        metrics = build()
        metrics.record_audit_entry("risk")
        metrics.record_audit_entry("risk")
        metrics.record_audit_entry("order")
        assert sample_value(metrics, "tradingsys_audit_entries_total", category="risk") == 2.0
        assert sample_value(metrics, "tradingsys_audit_entries_total", category="order") == 1.0

    def test_decisions_are_counted_by_category_and_outcome(self) -> None:
        metrics = build()
        metrics.record_decision("risk", "rejected")
        assert (
            sample_value(metrics, "tradingsys_decisions_total", category="risk", outcome="rejected")
            == 1.0
        )


class TestExposition:
    def test_render_is_parseable(self) -> None:
        rendered = build().render().decode()
        families = list(text_string_to_metric_families(rendered))
        assert families

    def test_every_metric_has_help_text(self) -> None:
        # An unhelped metric is unusable to whoever is on call at 3am.
        rendered = build().render().decode()
        for family in text_string_to_metric_families(rendered):
            assert family.documentation, f"{family.name} has no HELP text"

    def test_every_metric_is_namespaced(self) -> None:
        rendered = build().render().decode()
        for family in text_string_to_metric_families(rendered):
            assert family.name.startswith("tradingsys_"), family.name

    def test_the_content_type_matches_the_rendered_format(self) -> None:
        # Declaring the OpenMetrics content type while rendering the classic text
        # format makes Prometheus reject every scrape with "data does not end with
        # # EOF". The declared type and the body must agree.
        rendered = build().render().decode()
        if "openmetrics" in METRICS_CONTENT_TYPE:
            assert rendered.endswith("# EOF\n")
        else:
            assert "text/plain" in METRICS_CONTENT_TYPE
            assert not rendered.endswith("# EOF\n")

    def test_the_exposition_ends_with_a_newline(self) -> None:
        assert build().render().decode().endswith("\n")


class TestLabelCardinality:
    def test_no_metric_is_labelled_by_an_unbounded_value(self) -> None:
        # Labelling by correlation id, order id, or timestamp creates a new time series
        # per event and eventually takes the monitoring system down.
        forbidden = {"correlation_id", "order_id", "client_order_id", "timestamp", "ts", "id"}
        rendered = build().render().decode()
        for family in text_string_to_metric_families(rendered):
            for sample in family.samples:
                offending = forbidden & set(sample.labels)
                assert not offending, f"{sample.name} is labelled by {offending}"
