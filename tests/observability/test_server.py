"""Tests for the operational HTTP endpoints.

Requests go through the real ASGI app with httpx, so what is asserted is the status
codes, headers, and bodies a probe or a scraper would actually receive.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, final

import httpx
import pytest

from tradingsys.config.settings import LogFormat, LogLevel, ObservabilitySettings
from tradingsys.observability.correlation import (
    CORRELATION_ID_HEADER,
    current_correlation_id,
)
from tradingsys.observability.health import (
    CheckResult,
    HealthCheck,
    HealthRegistry,
    HealthStatus,
)
from tradingsys.observability.metrics import Metrics
from tradingsys.observability.server import build_operational_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.applications import Starlette

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_UNAVAILABLE = 503


@final
class ScriptedCheck(HealthCheck):
    """A check with a fixed outcome, used to drive the endpoints."""

    def __init__(self, name: str, status: HealthStatus = HealthStatus.PASS) -> None:
        self._name = name
        self._status = status
        self.seen_correlation_ids: list[str | None] = []

    @property
    def name(self) -> str:
        return self._name

    async def check(self) -> CheckResult:
        self.seen_correlation_ids.append(current_correlation_id())
        return CheckResult(name=self._name, status=self._status, duration_seconds=0.001)


def settings(**overrides: Any) -> ObservabilitySettings:
    defaults: dict[str, Any] = {
        "service_name": "tradingsys",
        "log_level": LogLevel.INFO,
        "log_format": LogFormat.JSON,
        "http_host": "127.0.0.1",
        "http_port": 8000,
        "health_path": "/health",
        "ready_path": "/ready",
        "metrics_path": "/metrics",
        "readiness_timeout_seconds": 3.0,
    }
    defaults.update(overrides)
    return ObservabilitySettings(**defaults)


def build_app(
    *,
    readiness: HealthRegistry | None = None,
    liveness: HealthRegistry | None = None,
    metrics: Metrics | None = None,
    observability: ObservabilitySettings | None = None,
) -> Starlette:
    return build_operational_app(
        observability or settings(),
        readiness=readiness or HealthRegistry(timeout_seconds=1.0),
        metrics=metrics
        or Metrics.create(service="tradingsys", environment="test", version="0.0.0"),
        liveness=liveness,
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app()), base_url="http://operational"
    ) as http:
        yield http


async def request(app: Starlette, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://operational"
    ) as http:
        return await http.get(path, **kwargs)


class TestHealthEndpoint:
    async def test_liveness_passes_with_no_checks(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/health")
        assert response.status_code == HTTP_OK
        assert response.json()["status"] == "pass"

    async def test_liveness_does_not_depend_on_readiness(self) -> None:
        # A failing dependency must not make the process look dead: restarting it does
        # not bring the dependency back.
        readiness = HealthRegistry(timeout_seconds=1.0)
        readiness.register(ScriptedCheck("database", HealthStatus.FAIL))
        app = build_app(readiness=readiness)
        assert (await request(app, "/health")).status_code == HTTP_OK
        assert (await request(app, "/ready")).status_code == HTTP_UNAVAILABLE

    async def test_liveness_checks_are_used_when_supplied(self) -> None:
        liveness = HealthRegistry(timeout_seconds=1.0)
        liveness.register(ScriptedCheck("event_loop", HealthStatus.FAIL))
        response = await request(build_app(liveness=liveness), "/health")
        assert response.status_code == HTTP_UNAVAILABLE
        assert response.json()["checks"][0]["name"] == "event_loop"

    async def test_the_response_carries_a_timestamp(self, client: httpx.AsyncClient) -> None:
        stamp = (await client.get("/health")).json()["time"]
        assert datetime.fromisoformat(stamp).tzinfo is not None


class TestReadyEndpoint:
    async def test_ready_with_all_checks_passing(self) -> None:
        readiness = HealthRegistry(timeout_seconds=1.0)
        readiness.register(ScriptedCheck("database"))
        readiness.register(ScriptedCheck("redis"))
        response = await request(build_app(readiness=readiness), "/ready")
        assert response.status_code == HTTP_OK
        body = response.json()
        assert body["status"] == "pass"
        assert {check["name"] for check in body["checks"]} == {"database", "redis"}

    async def test_not_ready_returns_503(self) -> None:
        readiness = HealthRegistry(timeout_seconds=1.0)
        readiness.register(ScriptedCheck("database", HealthStatus.FAIL))
        response = await request(build_app(readiness=readiness), "/ready")
        assert response.status_code == HTTP_UNAVAILABLE
        assert response.json()["status"] == "fail"

    async def test_the_failing_check_is_named(self) -> None:
        readiness = HealthRegistry(timeout_seconds=1.0)
        readiness.register(ScriptedCheck("database"))
        readiness.register(ScriptedCheck("redis", HealthStatus.FAIL))
        body = (await request(build_app(readiness=readiness), "/ready")).json()
        failed = [check for check in body["checks"] if check["status"] == "fail"]
        assert [check["name"] for check in failed] == ["redis"]

    async def test_readiness_updates_the_metric(self) -> None:
        metrics = Metrics.create(service="tradingsys", environment="test", version="0.0.0")
        readiness = HealthRegistry(timeout_seconds=1.0, metrics=metrics)
        readiness.register(ScriptedCheck("database"))
        app = build_app(readiness=readiness, metrics=metrics)
        await request(app, "/ready")
        assert "tradingsys_ready 1.0" in (await request(app, "/metrics")).text


class TestMetricsEndpoint:
    async def test_returns_the_exposition_format(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics")
        assert response.status_code == HTTP_OK
        assert "tradingsys_build_info" in response.text

    async def test_the_content_type_is_set(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics")
        assert response.headers["content-type"].startswith(
            ("text/plain", "application/openmetrics")
        )

    async def test_scraping_does_not_reset_counters(self) -> None:
        metrics = Metrics.create(service="tradingsys", environment="test", version="0.0.0")
        metrics.record_decision("risk", "accepted")
        app = build_app(metrics=metrics)
        first = await request(app, "/metrics")
        second = await request(app, "/metrics")
        assert 'tradingsys_decisions_total{category="risk",outcome="accepted"} 1.0' in first.text
        assert 'tradingsys_decisions_total{category="risk",outcome="accepted"} 1.0' in second.text


class TestConfigurablePaths:
    async def test_paths_come_from_configuration(self) -> None:
        app = build_app(
            observability=settings(
                health_path="/livez", ready_path="/readyz", metrics_path="/prometheus"
            )
        )
        assert (await request(app, "/livez")).status_code == HTTP_OK
        assert (await request(app, "/readyz")).status_code == HTTP_OK
        assert (await request(app, "/prometheus")).status_code == HTTP_OK
        assert (await request(app, "/health")).status_code == HTTP_NOT_FOUND

    async def test_an_unknown_path_explains_what_this_port_serves(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/orders")
        assert response.status_code == HTTP_NOT_FOUND
        assert "/health" in response.text
        assert "/metrics" in response.text

    async def test_this_port_exposes_nothing_else(self, client: httpx.AsyncClient) -> None:
        for path in ("/", "/orders", "/positions", "/admin"):
            assert (await client.get(path)).status_code == HTTP_NOT_FOUND


class TestCorrelationIds:
    async def test_a_generated_id_is_returned(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/health")
        assert len(response.headers[CORRELATION_ID_HEADER]) == 32

    async def test_an_inbound_id_is_adopted_and_echoed(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/health", headers={CORRELATION_ID_HEADER: "trace-abc"})
        assert response.headers[CORRELATION_ID_HEADER] == "trace-abc"

    async def test_the_id_is_visible_to_the_checks(self) -> None:
        check = ScriptedCheck("database")
        readiness = HealthRegistry(timeout_seconds=1.0)
        readiness.register(check)
        await request(
            build_app(readiness=readiness),
            "/ready",
            headers={CORRELATION_ID_HEADER: "trace-xyz"},
        )
        assert check.seen_correlation_ids == ["trace-xyz"]

    async def test_each_request_gets_its_own_id(self, client: httpx.AsyncClient) -> None:
        first = await client.get("/health")
        second = await client.get("/health")
        assert first.headers[CORRELATION_ID_HEADER] != second.headers[CORRELATION_ID_HEADER]

    async def test_the_id_does_not_leak_between_requests(self) -> None:
        check = ScriptedCheck("database")
        readiness = HealthRegistry(timeout_seconds=1.0)
        readiness.register(check)
        app = build_app(readiness=readiness)
        await request(app, "/ready", headers={CORRELATION_ID_HEADER: "first"})
        await request(app, "/ready", headers={CORRELATION_ID_HEADER: "second"})
        assert check.seen_correlation_ids == ["first", "second"]
        assert current_correlation_id() is None

    async def test_the_header_is_present_on_a_failure_response(self) -> None:
        readiness = HealthRegistry(timeout_seconds=1.0)
        readiness.register(ScriptedCheck("database", HealthStatus.FAIL))
        response = await request(build_app(readiness=readiness), "/ready")
        assert response.status_code == HTTP_UNAVAILABLE
        assert CORRELATION_ID_HEADER in response.headers


class TestResponseShape:
    async def test_the_ready_body_is_machine_readable(self) -> None:
        readiness = HealthRegistry(timeout_seconds=1.0)
        readiness.register(ScriptedCheck("database"))
        body = json.loads((await request(build_app(readiness=readiness), "/ready")).text)
        assert set(body) == {"status", "duration_ms", "checks", "time"}
        assert set(body["checks"][0]) >= {"name", "status", "duration_ms"}
