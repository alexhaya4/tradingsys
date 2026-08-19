"""Supervision, and the failure a liveness check cannot see.

The test that matters most here is the one where nothing raises: an activity whose task
is alive, whose exception count is zero, and which has stopped doing anything. That is
the shape of the capture drift recorded in `docs/DECISIONS.md`, where every check said
healthy for nine hours, and it is the shape an ingest process fails in.

Time is driven by an injected clock rather than by sleeping, so a stall of hours is
tested in microseconds and the test does not measure the machine's timer.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from tradingsys.app.supervisor import ProgressCheck, SupervisedActivity, Supervisor
from tradingsys.core.clock import Clock
from tradingsys.core.errors import TradingSysError

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.asyncio

START = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


class SteppableClock(Clock):
    """A clock that moves only when a test moves it.

    Wall clock time is the quantity under test, so it cannot also be the thing the test
    waits on. Stepping it explicitly is what lets a nine hour stall be asserted without
    a nine hour test, and it is also the honest model of the failure: on a suspended
    host, wall clock time advances while the process does nothing.
    """

    def __init__(self, start: datetime = START) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def activity(
    name: str,
    run: Callable[[Callable[[], None]], object],
    *,
    deadline: timedelta = timedelta(seconds=30),
) -> SupervisedActivity:
    return SupervisedActivity(
        name=name,
        run=run,  # type: ignore[arg-type]
        progress_deadline=deadline,
        restart_backoff=0.001,
        max_restart_backoff=0.01,
    )


class TestTheStallThatRaisesNothing:
    async def test_an_alive_activity_that_stops_progressing_is_stalled(self) -> None:
        # The defect class, made a test. The task is running, it has never raised, and
        # it is doing nothing. Every liveness check passes; this one must not.
        clock = SteppableClock()
        supervisor = Supervisor(clock)

        async def idles(progress: Callable[[], None]) -> None:
            progress()
            await asyncio.Event().wait()  # alive forever, reports nothing further

        supervisor.register(activity("stream", idles, deadline=timedelta(seconds=30)))
        supervisor.start()
        await asyncio.sleep(0)

        assert supervisor.stalled() == ()
        state = supervisor.states["stream"]
        assert state.failures == 0

        clock.advance(timedelta(seconds=31))

        assert supervisor.stalled() == ("stream",)
        assert supervisor.states["stream"].failures == 0, (
            "the stall must be detected without any exception having occurred"
        )
        await supervisor.stop()

    async def test_progress_clears_a_stall(self) -> None:
        clock = SteppableClock()
        supervisor = Supervisor(clock)
        ticked = asyncio.Event()

        async def ticks(progress: Callable[[], None]) -> None:
            while True:
                progress()
                ticked.set()
                await asyncio.sleep(0)

        supervisor.register(activity("stream", ticks, deadline=timedelta(seconds=5)))
        supervisor.start()
        await asyncio.wait_for(ticked.wait(), timeout=1)

        clock.advance(timedelta(seconds=6))
        assert supervisor.stalled() == ("stream",)

        # The work reports again at the new clock reading.
        ticked.clear()
        await asyncio.wait_for(ticked.wait(), timeout=1)
        assert supervisor.stalled() == ()
        await supervisor.stop()

    async def test_the_readiness_check_fails_on_a_stall_and_says_which(self) -> None:
        clock = SteppableClock()
        supervisor = Supervisor(clock)

        async def idles(progress: Callable[[], None]) -> None:
            progress()
            await asyncio.Event().wait()

        supervisor.register(activity("quotes", idles, deadline=timedelta(seconds=10)))
        supervisor.start()
        await asyncio.sleep(0)

        result = await ProgressCheck(supervisor).check()
        assert result.passed, result.detail

        clock.advance(timedelta(hours=9))
        result = await ProgressCheck(supervisor).check()

        assert not result.passed
        assert result.detail is not None
        assert "quotes" in result.detail
        await supervisor.stop()


class TestRestarting:
    async def test_an_activity_that_raises_is_restarted(self) -> None:
        clock = SteppableClock()
        supervisor = Supervisor(clock)
        attempts = 0
        third = asyncio.Event()

        async def fails_twice(progress: Callable[[], None]) -> None:
            nonlocal attempts
            attempts += 1
            if attempts >= 3:
                progress()
                third.set()
                await asyncio.Event().wait()
            raise ConnectionResetError("the venue dropped the socket")

        supervisor.register(activity("stream", fails_twice))
        supervisor.start()
        await asyncio.wait_for(third.wait(), timeout=2)

        state = supervisor.states["stream"]
        assert state.starts >= 3
        assert state.failures == 2
        assert "ConnectionResetError" in str(state.last_error)
        await supervisor.stop()

    async def test_an_activity_that_returns_cleanly_is_also_restarted(self) -> None:
        # A stream that ends is not a stream that finished. Treating a clean return as
        # completion is how ingestion stops on a process that stays up.
        clock = SteppableClock()
        supervisor = Supervisor(clock)
        starts = 0
        twice = asyncio.Event()

        async def returns(progress: Callable[[], None]) -> None:
            nonlocal starts
            starts += 1
            progress()
            if starts >= 2:
                twice.set()

        supervisor.register(activity("stream", returns))
        supervisor.start()
        await asyncio.wait_for(twice.wait(), timeout=2)

        assert supervisor.states["stream"].starts >= 2
        assert supervisor.states["stream"].failures == 0
        await supervisor.stop()

    async def test_cancellation_stops_an_activity_rather_than_restarting_it(self) -> None:
        clock = SteppableClock()
        supervisor = Supervisor(clock)

        async def idles(progress: Callable[[], None]) -> None:
            progress()
            await asyncio.Event().wait()

        supervisor.register(activity("stream", idles))
        supervisor.start()
        await asyncio.sleep(0)
        starts_before = supervisor.states["stream"].starts

        await supervisor.stop()
        await asyncio.sleep(0.01)

        assert supervisor.states["stream"].starts == starts_before
        assert not supervisor.states["stream"].running

    async def test_stop_is_idempotent(self) -> None:
        supervisor = Supervisor(SteppableClock())

        async def idles(progress: Callable[[], None]) -> None:
            progress()
            await asyncio.Event().wait()

        supervisor.register(activity("stream", idles))
        supervisor.start()
        await asyncio.sleep(0)
        await supervisor.stop()
        await supervisor.stop()


class TestRegistration:
    async def test_a_duplicate_name_is_refused(self) -> None:
        # Two activities under one name would share a progress record, so one could mask
        # the other's stall.
        supervisor = Supervisor(SteppableClock())

        async def idles(progress: Callable[[], None]) -> None:
            progress()

        supervisor.register(activity("stream", idles))
        with pytest.raises(TradingSysError, match="already registered"):
            supervisor.register(activity("stream", idles))

    async def test_an_activity_without_a_deadline_is_refused(self) -> None:
        # An activity with no deadline cannot be found to have stalled, which is the
        # failure this supervisor exists to detect.
        async def idles(progress: Callable[[], None]) -> None:
            progress()

        with pytest.raises(TradingSysError, match="progress_deadline must be positive"):
            SupervisedActivity(
                name="stream",
                run=idles,
                progress_deadline=timedelta(0),
                restart_backoff=1.0,
                max_restart_backoff=2.0,
            )

    async def test_a_backoff_ceiling_below_the_base_is_refused(self) -> None:
        async def idles(progress: Callable[[], None]) -> None:
            progress()

        with pytest.raises(TradingSysError, match="max_restart_backoff"):
            SupervisedActivity(
                name="stream",
                run=idles,
                progress_deadline=timedelta(seconds=5),
                restart_backoff=10.0,
                max_restart_backoff=1.0,
            )

    async def test_a_supervisor_with_nothing_registered_is_healthy(self) -> None:
        result = await ProgressCheck(Supervisor(SteppableClock())).check()
        assert result.passed
        assert result.detail is not None
        assert "no activities" in result.detail
