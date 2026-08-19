"""Supervision for long lived work, where being alive is not evidence of working.

`docs/DECISIONS.md` records the defect class this module exists to answer: a check that
confirms a process exists proves nothing about whether it will act, or act at the right
time. That was found on a capture that drifted nine hours while every liveness check
passed, and it binds here more than anywhere, because an ingest process that is running
and receiving nothing looks identical to one that is running and receiving everything.

So supervision here has two halves and the second is the one that matters.

**Restart what dies.** A task that raises is restarted with exponential backoff and
jitter, and the failure is counted and kept. A task that returns is also restarted,
because a stream that ends cleanly has still stopped ingesting. Nothing is left dead
quietly.

**Report on progress, not on liveness.** Every supervised activity declares how long it
may go without making progress. The supervisor records the wall clock instant of the
last progress each one reported, and a task that has not reported within its deadline is
unhealthy **even though its task object is alive and its exception count is zero**. That
is the whole point: the failure mode being guarded against is the one where nothing
raises.

Progress is reported by the work itself, because only the work knows what progress means.
A quote stream makes progress when a quote arrives, not when its loop iterates.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from tradingsys.core.errors import TradingSysError
from tradingsys.observability.health import CheckResult, HealthCheck, HealthStatus
from tradingsys.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime, timedelta

    from tradingsys.core.clock import Clock

__all__ = [
    "ActivityState",
    "ProgressCheck",
    "SupervisedActivity",
    "Supervisor",
]

logger = get_logger("app.supervisor")


@final
@dataclass(slots=True)
class ActivityState:
    """What the supervisor knows about one activity.

    Attributes:
        name: Identifier used in logs, metrics, and health output.
        last_progress: When the work last reported progress, by the injected clock.
        starts: How many times the activity has been started, including the first.
        failures: How many times it has raised.
        last_error: The most recent failure, kept as type and message.
        running: Whether a task for it currently exists.
    """

    name: str
    last_progress: datetime
    starts: int = 0
    failures: int = 0
    last_error: str | None = None
    running: bool = False


@final
@dataclass(frozen=True, slots=True)
class SupervisedActivity:
    """One long lived job and the terms it is supervised on.

    Attributes:
        name: Identifier.
        run: Coroutine factory. Called afresh on every start, because a coroutine
            cannot be awaited twice and a restart needs a new one.
        progress_deadline: Longest the activity may go without reporting progress before
            it is considered stalled. Sized against what the work actually does: a
            crypto quote stream should report within seconds, a periodic backfill within
            rather more than its interval.
        restart_backoff: First delay after a failure. Doubles per consecutive failure,
            with jitter, and resets once the activity reports progress again.
        max_restart_backoff: Ceiling on that delay, so a venue outage does not push the
            retry interval past the point of usefulness.
    """

    name: str
    run: Callable[[Callable[[], None]], Awaitable[None]]
    progress_deadline: timedelta
    restart_backoff: float
    max_restart_backoff: float

    def __post_init__(self) -> None:
        if self.restart_backoff <= 0 or self.max_restart_backoff < self.restart_backoff:
            raise TradingSysError(
                f"{self.name}: restart_backoff must be positive and not exceed "
                f"max_restart_backoff, got {self.restart_backoff} and "
                f"{self.max_restart_backoff}"
            )
        if self.progress_deadline.total_seconds() <= 0:
            raise TradingSysError(
                f"{self.name}: progress_deadline must be positive. An activity with no "
                f"deadline cannot be found to have stalled, which is the failure this "
                f"supervisor exists to detect."
            )


@final
class Supervisor:
    """Runs activities, restarts them, and reports whether they are making progress."""

    __slots__ = ("_activities", "_clock", "_jitter", "_sleep", "_states", "_tasks")

    def __init__(
        self,
        clock: Clock,
        *,
        jitter: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._clock = clock
        self._jitter = jitter if jitter is not None else random.Random()
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._activities: dict[str, SupervisedActivity] = {}
        self._states: dict[str, ActivityState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def register(self, activity: SupervisedActivity) -> None:
        """Add an activity. Must happen before :meth:`start`.

        Raises:
            TradingSysError: The name is already registered. Two activities under one
                name would share a progress record, so one could mask the other's stall.
        """
        if activity.name in self._activities:
            raise TradingSysError(f"an activity named {activity.name!r} is already registered")
        self._activities[activity.name] = activity
        self._states[activity.name] = ActivityState(
            name=activity.name, last_progress=self._clock.now()
        )

    @property
    def states(self) -> dict[str, ActivityState]:
        """A snapshot of every activity's state."""
        return dict(self._states)

    def stalled(self) -> tuple[str, ...]:
        """Activities that have not reported progress within their deadline.

        This is the question worth asking. An activity appears here while its task is
        alive and its failure count is zero, which is exactly the case a liveness check
        misses.
        """
        now = self._clock.now()
        return tuple(
            name
            for name, state in sorted(self._states.items())
            if now - state.last_progress > self._activities[name].progress_deadline
        )

    def start(self) -> None:
        """Start every registered activity."""
        for name in self._activities:
            if name not in self._tasks:
                self._tasks[name] = asyncio.create_task(
                    self._supervise(name), name=f"supervise-{name}"
                )

    async def stop(self) -> None:
        """Cancel every activity and wait for it. Safe to call more than once."""
        for task in self._tasks.values():
            task.cancel()
        for name, task in list(self._tasks.items()):
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._states[name].running = False
        self._tasks.clear()

    async def _supervise(self, name: str) -> None:
        """Run one activity forever, restarting it on failure or return."""
        activity = self._activities[name]
        state = self._states[name]
        backoff = activity.restart_backoff

        while True:
            state.starts += 1
            state.running = True
            progress_before = state.last_progress
            try:
                await activity.run(lambda: self._record_progress(name))
            except asyncio.CancelledError:
                state.running = False
                raise
            except Exception as exc:
                state.failures += 1
                state.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "supervised activity failed",
                    activity=name,
                    starts=state.starts,
                    failures=state.failures,
                    error=state.last_error,
                )
            else:
                # Returning is not success. A stream that ends has stopped ingesting,
                # and treating a clean return as completion is how ingestion silently
                # stops on a process that stays up.
                logger.warning(
                    "supervised activity returned, which is not completion",
                    activity=name,
                    starts=state.starts,
                )
            state.running = False

            # An activity that made progress before failing was working, so its next
            # failure starts from the base delay again. Without this, one bad hour
            # leaves the retry interval at its ceiling for the rest of the run.
            if state.last_progress > progress_before:
                backoff = activity.restart_backoff

            delay = self._jitter.uniform(0.0, backoff)
            await self._sleep(delay)
            backoff = min(backoff * 2, activity.max_restart_backoff)

    def _record_progress(self, name: str) -> None:
        """Note that an activity did something. Called by the work, not by the loop."""
        self._states[name].last_progress = self._clock.now()


@final
class ProgressCheck(HealthCheck):
    """A readiness check that fails when supervised work has stalled.

    This is the defect class made executable. It deliberately does not ask whether any
    task is alive, because that question passed twice on work that had drifted nine
    hours. It asks when each activity last made progress, and compares that to the wall
    clock now.

    Registered on readiness rather than liveness: a stalled ingest should stop the
    process being treated as ready to serve, and should not by itself trigger a restart,
    since restarting a process whose venue is down achieves nothing.

    It subclasses :class:`~tradingsys.observability.health.HealthCheck` because the
    registry takes that and nothing else. The first version returned a bare tuple and
    was never registered anywhere, so the mismatch went unnoticed until the assembly
    tried to use it, which is the same shape of defect as the components it supervises.
    """

    __slots__ = ("_supervisor",)

    def __init__(self, supervisor: Supervisor) -> None:
        self._supervisor = supervisor

    @property
    def name(self) -> str:
        return "ingest_progress"

    async def check(self) -> CheckResult:
        """Whether every activity is within its progress deadline, and the detail."""
        stalled = self._supervisor.stalled()
        states = self._supervisor.states
        if stalled:
            detail = "; ".join(
                f"{name} last progressed {states[name].last_progress.isoformat()}"
                for name in stalled
            )
            return CheckResult(
                name=self.name,
                status=HealthStatus.FAIL,
                duration_seconds=0.0,
                detail=f"stalled: {detail}",
            )
        return CheckResult(
            name=self.name,
            status=HealthStatus.PASS,
            duration_seconds=0.0,
            detail=(
                f"{len(states)} activities progressing" if states else "no activities registered"
            ),
        )
