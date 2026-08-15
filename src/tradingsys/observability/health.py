"""Liveness and readiness checks.

The distinction matters operationally and is easy to conflate.

*Liveness* answers "should this process be restarted?" It must not depend on anything
external: if the database is down, restarting the trading process does not help and
restarting it repeatedly makes the outage worse.

*Readiness* answers "should this process be given work?" It does depend on external
dependencies, because a process that cannot reach its database or its venue must not
be routed to.

Checks run concurrently under a shared deadline. A check that hangs is reported as a
failure at the deadline rather than blocking the endpoint, because a readiness probe
that never answers is treated as a failure anyway, just more slowly and less clearly.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, final

if TYPE_CHECKING:
    from collections.abc import Sequence

    from redis.asyncio import Redis

    from tradingsys.observability.metrics import Metrics
    from tradingsys.persistence.database import Database

__all__ = [
    "CheckResult",
    "DatabaseCheck",
    "HealthCheck",
    "HealthRegistry",
    "HealthReport",
    "HealthStatus",
    "RedisCheck",
]


class HealthStatus(StrEnum):
    """Outcome of a check or of an aggregate report."""

    PASS = "pass"
    FAIL = "fail"


@final
@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one check."""

    name: str
    status: HealthStatus
    duration_seconds: float
    detail: str | None = None
    """Why it failed, or a useful fact when it passed, such as a server version."""

    @property
    def passed(self) -> bool:
        return self.status is HealthStatus.PASS

    def to_mapping(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status.value,
            "duration_ms": round(self.duration_seconds * 1000, 3),
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@final
@dataclass(frozen=True, slots=True)
class HealthReport:
    """The aggregate outcome of a set of checks."""

    status: HealthStatus
    checks: tuple[CheckResult, ...] = ()
    duration_seconds: float = 0.0

    @property
    def healthy(self) -> bool:
        return self.status is HealthStatus.PASS

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "duration_ms": round(self.duration_seconds * 1000, 3),
            "checks": [check.to_mapping() for check in self.checks],
        }


class HealthCheck(ABC):
    """One dependency's readiness check."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short, stable identifier, used as a metric label and in the response body."""
        raise NotImplementedError

    @abstractmethod
    async def check(self) -> CheckResult:
        """Evaluate the dependency.

        Implementations should not raise: a failure is a result, not an exception.
        The registry catches anything that escapes anyway, so that one badly behaved
        check cannot take down the endpoint.
        """
        raise NotImplementedError


@final
class DatabaseCheck(HealthCheck):
    """Verifies the database answers a trivial query."""

    __slots__ = ("_database",)

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def name(self) -> str:
        return "database"

    async def check(self) -> CheckResult:
        started = time.perf_counter()
        try:
            await self._database.check()
        except Exception as exc:
            return CheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                duration_seconds=time.perf_counter() - started,
                detail=str(exc),
            )
        stats = self._database.pool_stats()
        return CheckResult(
            name=self.name,
            status=HealthStatus.PASS,
            duration_seconds=time.perf_counter() - started,
            detail=f"pool {stats['in_use']}/{stats['max_size']} in use",
        )


@final
class RedisCheck(HealthCheck):
    """Verifies Redis answers a PING."""

    __slots__ = ("_client",)

    def __init__(self, client: Redis) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "redis"

    async def check(self) -> CheckResult:
        started = time.perf_counter()
        try:
            answered = await self._client.ping()
        except Exception as exc:
            return CheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                duration_seconds=time.perf_counter() - started,
                detail=str(exc),
            )
        duration = time.perf_counter() - started
        if not answered:
            return CheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                duration_seconds=duration,
                detail="PING was not acknowledged",
            )
        return CheckResult(name=self.name, status=HealthStatus.PASS, duration_seconds=duration)


@final
@dataclass(slots=True)
class HealthRegistry:
    """Runs a set of checks concurrently under a deadline.

    Attributes:
        timeout_seconds: Deadline shared by all checks in one evaluation.
        metrics: Optional metric set to record outcomes on.
    """

    timeout_seconds: float
    metrics: Metrics | None = None
    _checks: list[HealthCheck] = field(default_factory=list)

    def register(self, check: HealthCheck) -> HealthCheck:
        """Add a check.

        Raises:
            ValueError: A check with the same name is already registered, which would
                make the report ambiguous and the metric labels collide.
        """
        if any(existing.name == check.name for existing in self._checks):
            raise ValueError(f"a health check named {check.name!r} is already registered")
        self._checks.append(check)
        return check

    @property
    def checks(self) -> Sequence[HealthCheck]:
        return tuple(self._checks)

    async def evaluate(self) -> HealthReport:
        """Run every check and aggregate the results.

        A check that raises, or that exceeds the deadline, is reported as a failure
        with an explanation rather than propagating.
        """
        started = time.perf_counter()
        if not self._checks:
            return HealthReport(status=HealthStatus.PASS, checks=(), duration_seconds=0.0)

        results = await asyncio.gather(
            *(self._run_one(check) for check in self._checks), return_exceptions=False
        )
        duration = time.perf_counter() - started
        status = (
            HealthStatus.PASS if all(result.passed for result in results) else HealthStatus.FAIL
        )
        if self.metrics is not None:
            self.metrics.set_ready(status is HealthStatus.PASS)
        return HealthReport(status=status, checks=tuple(results), duration_seconds=duration)

    async def _run_one(self, check: HealthCheck) -> CheckResult:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await check.check()
        except TimeoutError:
            result = CheckResult(
                name=check.name,
                status=HealthStatus.FAIL,
                duration_seconds=time.perf_counter() - started,
                detail=f"did not answer within {self.timeout_seconds}s",
            )
        except Exception as exc:
            result = CheckResult(
                name=check.name,
                status=HealthStatus.FAIL,
                duration_seconds=time.perf_counter() - started,
                detail=f"{type(exc).__name__}: {exc}",
            )
        if self.metrics is not None:
            self.metrics.health_checks.labels(check=result.name, status=result.status.value).inc()
            self.metrics.health_check_duration.labels(check=result.name).observe(
                result.duration_seconds
            )
        return result
