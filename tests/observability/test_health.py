"""Tests for the health check registry and the real dependency checks."""

from __future__ import annotations

import asyncio
import json
from typing import final

import pytest

from tradingsys.core.errors import PersistenceError
from tradingsys.observability.health import (
    CheckResult,
    DatabaseCheck,
    HealthCheck,
    HealthRegistry,
    HealthStatus,
    RedisCheck,
)
from tradingsys.observability.metrics import Metrics


@final
class ScriptedCheck(HealthCheck):
    """A check whose behaviour the test dictates.

    Not a stand-in for a real dependency: it exists to drive the registry through
    passing, failing, raising, and hanging branches, which real dependencies cannot be
    made to do on demand.
    """

    def __init__(
        self,
        name: str,
        *,
        status: HealthStatus = HealthStatus.PASS,
        delay: float = 0.0,
        raises: Exception | None = None,
        detail: str | None = None,
    ) -> None:
        self._name = name
        self._status = status
        self._delay = delay
        self._raises = raises
        self._detail = detail
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def check(self) -> CheckResult:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return CheckResult(
            name=self._name, status=self._status, duration_seconds=0.0, detail=self._detail
        )


@final
class FakeDatabase:
    """The subset of Database that DatabaseCheck uses."""

    def __init__(self, *, fails: Exception | None = None) -> None:
        self._fails = fails

    async def check(self) -> None:
        if self._fails is not None:
            raise self._fails

    def pool_stats(self) -> dict[str, int]:
        return {"size": 3, "idle": 1, "in_use": 2, "max_size": 10}


@final
class FakeRedis:
    """The subset of the Redis client that RedisCheck uses."""

    def __init__(self, *, answer: bool = True, fails: Exception | None = None) -> None:
        self._answer = answer
        self._fails = fails

    async def ping(self) -> bool:
        if self._fails is not None:
            raise self._fails
        return self._answer


def registry(timeout: float = 1.0, metrics: Metrics | None = None) -> HealthRegistry:
    return HealthRegistry(timeout_seconds=timeout, metrics=metrics)


def fresh_metrics() -> Metrics:
    return Metrics.create(service="tradingsys", environment="test", version="0.0.0")


class TestRegistration:
    def test_checks_are_listed_in_registration_order(self) -> None:
        health = registry()
        health.register(ScriptedCheck("a"))
        health.register(ScriptedCheck("b"))
        assert [check.name for check in health.checks] == ["a", "b"]

    def test_duplicate_names_are_refused(self) -> None:
        health = registry()
        health.register(ScriptedCheck("database"))
        with pytest.raises(ValueError, match="already registered"):
            health.register(ScriptedCheck("database"))

    async def test_an_empty_registry_is_healthy(self) -> None:
        report = await registry().evaluate()
        assert report.healthy
        assert report.checks == ()


class TestEvaluation:
    async def test_all_passing(self) -> None:
        health = registry()
        health.register(ScriptedCheck("a"))
        health.register(ScriptedCheck("b"))
        report = await health.evaluate()
        assert report.healthy
        assert report.status is HealthStatus.PASS
        assert len(report.checks) == 2
        assert report.failures == ()

    async def test_one_failure_fails_the_report(self) -> None:
        health = registry()
        health.register(ScriptedCheck("a"))
        health.register(ScriptedCheck("b", status=HealthStatus.FAIL))
        report = await health.evaluate()
        assert not report.healthy
        assert [check.name for check in report.failures] == ["b"]

    async def test_every_check_runs_even_when_one_fails(self) -> None:
        first = ScriptedCheck("a", status=HealthStatus.FAIL)
        second = ScriptedCheck("b")
        health = registry()
        health.register(first)
        health.register(second)
        await health.evaluate()
        assert first.calls == 1
        assert second.calls == 1

    async def test_checks_run_concurrently(self) -> None:
        # Three checks that each sleep 100ms must finish in about 100ms, not 300ms.
        health = registry(timeout=2.0)
        for name in ("a", "b", "c"):
            health.register(ScriptedCheck(name, delay=0.1))
        report = await health.evaluate()
        assert report.healthy
        assert report.duration_seconds < 0.25

    async def test_a_raising_check_becomes_a_failure(self) -> None:
        health = registry()
        health.register(ScriptedCheck("a", raises=RuntimeError("exploded")))
        report = await health.evaluate()
        assert not report.healthy
        assert report.checks[0].detail == "RuntimeError: exploded"

    async def test_a_hanging_check_fails_at_the_deadline(self) -> None:
        health = registry(timeout=0.05)
        health.register(ScriptedCheck("slow", delay=5.0))
        report = await health.evaluate()
        assert not report.healthy
        assert "did not answer within" in (report.checks[0].detail or "")

    async def test_a_hanging_check_does_not_delay_the_others(self) -> None:
        health = registry(timeout=0.05)
        health.register(ScriptedCheck("slow", delay=5.0))
        health.register(ScriptedCheck("fast"))
        report = await health.evaluate()
        assert report.duration_seconds < 1.0
        assert {check.name: check.passed for check in report.checks} == {
            "slow": False,
            "fast": True,
        }

    async def test_durations_are_recorded(self) -> None:
        health = registry()
        health.register(ScriptedCheck("a", delay=0.01))
        report = await health.evaluate()
        assert report.checks[0].duration_seconds >= 0


class TestReportRendering:
    async def test_mapping_is_json_serialisable(self) -> None:
        health = registry()
        health.register(ScriptedCheck("a", detail="all good"))
        health.register(ScriptedCheck("b", status=HealthStatus.FAIL, detail="broken"))
        payload = json.loads(json.dumps((await health.evaluate()).to_mapping()))
        assert payload["status"] == "fail"
        assert {check["name"] for check in payload["checks"]} == {"a", "b"}

    async def test_detail_is_omitted_when_absent(self) -> None:
        health = registry()
        health.register(ScriptedCheck("a"))
        payload = (await health.evaluate()).to_mapping()
        assert "detail" not in payload["checks"][0]

    async def test_durations_are_reported_in_milliseconds(self) -> None:
        health = registry()
        health.register(ScriptedCheck("a"))
        payload = (await health.evaluate()).to_mapping()
        assert "duration_ms" in payload["checks"][0]


class TestMetricsIntegration:
    async def test_outcomes_are_counted(self) -> None:
        metrics = fresh_metrics()
        health = registry(metrics=metrics)
        health.register(ScriptedCheck("a"))
        health.register(ScriptedCheck("b", status=HealthStatus.FAIL))
        await health.evaluate()
        rendered = metrics.render().decode()
        assert 'tradingsys_health_checks_total{check="a",status="pass"} 1.0' in rendered
        assert 'tradingsys_health_checks_total{check="b",status="fail"} 1.0' in rendered

    async def test_the_ready_gauge_follows_the_report(self) -> None:
        metrics = fresh_metrics()
        health = registry(metrics=metrics)
        passing = ScriptedCheck("a")
        health.register(passing)
        await health.evaluate()
        assert "tradingsys_ready 1.0" in metrics.render().decode()

        failing = ScriptedCheck("b", status=HealthStatus.FAIL)
        health.register(failing)
        await health.evaluate()
        assert "tradingsys_ready 0.0" in metrics.render().decode()

    async def test_durations_are_observed(self) -> None:
        metrics = fresh_metrics()
        health = registry(metrics=metrics)
        health.register(ScriptedCheck("a"))
        await health.evaluate()
        assert 'tradingsys_health_check_duration_seconds_count{check="a"} 1.0' in (
            metrics.render().decode()
        )


class TestDatabaseCheck:
    async def test_passing(self) -> None:
        check = DatabaseCheck(FakeDatabase())  # type: ignore[arg-type]
        result = await check.check()
        assert result.passed
        assert result.name == "database"
        assert result.detail == "pool 2/10 in use"

    async def test_failing(self) -> None:
        check = DatabaseCheck(FakeDatabase(fails=PersistenceError("connection refused")))  # type: ignore[arg-type]
        result = await check.check()
        assert not result.passed
        assert "connection refused" in (result.detail or "")

    async def test_a_failure_does_not_propagate(self) -> None:
        check = DatabaseCheck(FakeDatabase(fails=OSError("network down")))  # type: ignore[arg-type]
        result = await check.check()
        assert result.status is HealthStatus.FAIL


class TestRedisCheck:
    async def test_passing(self) -> None:
        result = await RedisCheck(FakeRedis()).check()  # type: ignore[arg-type]
        assert result.passed
        assert result.name == "redis"

    async def test_an_unacknowledged_ping_fails(self) -> None:
        result = await RedisCheck(FakeRedis(answer=False)).check()  # type: ignore[arg-type]
        assert not result.passed
        assert "not acknowledged" in (result.detail or "")

    async def test_a_connection_error_fails(self) -> None:
        result = await RedisCheck(FakeRedis(fails=OSError("refused"))).check()  # type: ignore[arg-type]
        assert not result.passed
        assert "refused" in (result.detail or "")


class TestLivenessIsIndependentOfDependencies:
    async def test_a_failing_dependency_does_not_affect_an_empty_liveness_registry(
        self,
    ) -> None:
        # Liveness must not be wired to dependencies: restarting the process does not
        # fix a database outage, and a restart loop makes the outage worse.
        readiness = registry()
        readiness.register(ScriptedCheck("database", status=HealthStatus.FAIL))
        liveness = registry()
        assert not (await readiness.evaluate()).healthy
        assert (await liveness.evaluate()).healthy
