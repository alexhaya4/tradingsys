"""Process lifecycle: start dependencies, serve, shut down cleanly.

Startup order is deliberate. Configuration is validated first, because everything else
needs it and a bad value must abort before anything connects. Logging comes next, so
that every subsequent step is observable. Then the dependencies, then the HTTP server.

Shutdown runs in reverse and is best effort: each step is attempted even if an earlier
one failed, because a process that cannot close its database pool must still stop
serving traffic. The startup audit entry and its matching shutdown entry bracket the
run in the audit log, so a restart is visible there and not only in the process
manager.

There is no trading in this phase. What runs is the skeleton those components will be
attached to.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self, final

import uvicorn
from redis.asyncio import Redis
from starlette.applications import Starlette

from tradingsys import __version__
from tradingsys.app.assembly import CRYPTO_VENUE, AssembledIngest, assemble_ingest
from tradingsys.core.clock import SystemClock
from tradingsys.core.currency import default_registry
from tradingsys.core.errors import TradingSysError
from tradingsys.observability.correlation import correlation_id
from tradingsys.observability.health import DatabaseCheck, HealthRegistry, RedisCheck
from tradingsys.observability.logging import configure_from_settings, get_logger
from tradingsys.observability.metrics import Metrics
from tradingsys.observability.server import build_operational_app
from tradingsys.persistence.audit import AuditCategory, AuditLog
from tradingsys.persistence.database import Database
from tradingsys.persistence.repositories import InstrumentRepository, MarketDataRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import TracebackType

    from structlog.stdlib import BoundLogger

    from tradingsys.config.settings import Settings
    from tradingsys.core.clock import Clock
    from tradingsys.core.currency import CurrencyRegistry

__all__ = ["Application", "run"]

_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)

REQUIRED_TABLES = ("instruments", "ohlcv_bars", "ticks", "audit_log")
"""Tables the process cannot run without. Checked at startup so that an unmigrated
database is reported as such rather than as an undefined-table error mid-operation."""


class _ServerWithoutSignalHandlers(uvicorn.Server):
    """A uvicorn server that leaves signal handling to the runtime.

    Shutdown has to be ordered: stop serving, then close the dependencies. uvicorn's
    own handlers would race that, so they are suppressed and :func:`serve` installs
    its own.
    """

    def install_signal_handlers(self) -> None:
        return


@final
@dataclass(slots=True)
class Application:
    """Everything one process owns, wired together.

    Construct with :meth:`build`, which does no I/O, then :meth:`start` to connect.
    Separating the two means a test can inspect the wiring without a database.
    """

    settings: Settings
    clock: Clock
    metrics: Metrics
    database: Database
    redis: Redis
    audit_log: AuditLog
    instruments: InstrumentRepository
    market_data: MarketDataRepository
    readiness: HealthRegistry
    currencies: CurrencyRegistry
    _started: bool = field(default=False, init=False)
    _ingest: AssembledIngest | None = field(default=None, init=False)

    @classmethod
    def build(cls, settings: Settings, clock: Clock | None = None) -> Self:
        """Wire the components together without connecting to anything."""
        resolved_clock = clock if clock is not None else SystemClock()
        metrics = Metrics.create(
            service=settings.observability.service_name,
            environment=settings.app.environment.value,
            version=__version__,
        )
        database = Database(settings.database)
        redis = Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            db=settings.redis.db,
            username=settings.redis.username,
            password=(
                settings.redis.password.get_secret_value()
                if settings.redis.password is not None
                else None
            ),
            socket_timeout=settings.redis.socket_timeout_seconds,
            socket_connect_timeout=settings.redis.connect_timeout_seconds,
            decode_responses=True,
        )
        readiness = HealthRegistry(
            timeout_seconds=settings.observability.readiness_timeout_seconds, metrics=metrics
        )
        readiness.register(DatabaseCheck(database))
        readiness.register(RedisCheck(redis))
        return cls(
            settings=settings,
            clock=resolved_clock,
            metrics=metrics,
            database=database,
            redis=redis,
            audit_log=AuditLog(database, resolved_clock),
            instruments=InstrumentRepository(database, default_registry),
            market_data=MarketDataRepository(database),
            readiness=readiness,
            currencies=default_registry,
        )

    @property
    def is_started(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Connect every dependency and record the startup in the audit log.

        Raises:
            PersistenceError: The database could not be reached. Nothing this system
                does is safe without its audit log, so a failure here aborts startup
                rather than degrading.
        """
        logger = get_logger("app.runtime")
        await self.database.connect()
        self.metrics.record_pool(self.database.pool_stats())

        timescale = await self.database.timescale_version()
        if timescale is None:
            raise TradingSysError(
                "TimescaleDB is not installed in this database; the bar and tick tables "
                "are hypertables and cannot work without it"
            )
        absent = await self.database.missing_tables(REQUIRED_TABLES)
        if absent:
            raise TradingSysError(
                f"the database is missing {', '.join(absent)}. Apply the migrations with "
                f"`alembic upgrade head` before starting the process."
            )
        logger.info("database connected", dsn=self.settings.database.dsn(), timescale=timescale)

        await self.redis.ping()
        logger.info("redis connected", url=self.settings.redis.url())

        with correlation_id() as trace:
            await self.audit_log.append(
                correlation_id=trace,
                category=AuditCategory.SYSTEM,
                actor="app.runtime",
                action="started",
                summary=(
                    f"process started in the {self.settings.app.environment.value} environment"
                ),
                payload={"version": __version__, "configuration": self.settings.describe()},
            )
            self.metrics.record_audit_entry(AuditCategory.SYSTEM.value)
        await self._start_ingest(logger)

        self._started = True
        logger.info(
            "startup complete",
            version=__version__,
            environment=self.settings.app.environment.value,
            live_venues=list(self.settings.venues.live_venue_names()),
        )

    async def _start_ingest(self, logger: BoundLogger) -> None:
        """Assemble and start ingestion, if this deployment records anything.

        Assembly happens here rather than in :meth:`build` because instrument row ids
        come from the database, and a recorder without them counts every quote as an
        unknown instrument and writes nothing.

        A deployment with no enabled crypto venue starts without ingesting, which is the
        correct behaviour for a process that only serves the operational endpoints. It
        says so rather than appearing to record.
        """
        crypto = self.settings.venues.crypto.get(CRYPTO_VENUE)
        if crypto is None or not crypto.enabled:
            logger.info(
                "ingestion is not enabled for this deployment, nothing will be recorded",
                venue=CRYPTO_VENUE,
            )
            return

        self._ingest = await assemble_ingest(
            self.settings,
            self.clock,
            database=self.database,
            instruments=self.instruments,
            market_data=self.market_data,
            audit_log=self.audit_log,
            currencies=self.currencies,
        )
        # Registered on readiness rather than liveness: a stalled ingest should stop this
        # process being given work and should not by itself trigger a restart, because
        # restarting a process whose venue is down achieves nothing.
        self.readiness.register(self._ingest.process.progress_check())
        self._ingest.process.register_activities()
        self._ingest.process.start()

    async def stop(self) -> None:
        """Close every dependency, attempting each one even if another fails."""
        logger = get_logger("app.runtime")
        if self._ingest is not None:
            # Before the database closes: stopping flushes the batch the recorder still
            # holds, and a batch that was accepted and not written is data this system
            # claimed to have recorded.
            try:
                await self._ingest.process.stop()
                await self._ingest.aclose()
            except Exception:
                logger.exception("failed to stop ingestion cleanly")
            self._ingest = None
        if self._started:
            try:
                with correlation_id() as trace:
                    await self.audit_log.append(
                        correlation_id=trace,
                        category=AuditCategory.SYSTEM,
                        actor="app.runtime",
                        action="stopped",
                        summary="process shutting down",
                        payload={"version": __version__},
                    )
            except Exception:
                logger.exception("could not record shutdown in the audit log")
        self._started = False

        for name, close in (
            ("redis", self.redis.aclose),
            ("database", self.database.close),
        ):
            try:
                await close()
            except Exception:
                logger.exception("failed to close a dependency", dependency=name)
        logger.info("shutdown complete")

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    def build_http_app(self) -> Starlette:
        """The operational ASGI app for this process."""
        return build_operational_app(
            self.settings.observability, readiness=self.readiness, metrics=self.metrics
        )

    async def refresh_pool_metrics(self, interval_seconds: float = 15.0) -> None:
        """Publish pool occupancy on an interval until cancelled."""
        while True:
            self.metrics.record_pool(self.database.pool_stats())
            await asyncio.sleep(interval_seconds)


async def serve(application: Application) -> None:
    """Run the operational server until a shutdown signal arrives.

    uvicorn's own signal handling is disabled so that shutdown is driven from here:
    the server stops first, then the dependencies close, in that order.
    """
    logger = get_logger("app.runtime")
    settings = application.settings

    config = uvicorn.Config(
        app=application.build_http_app(),
        host=settings.observability.http_host,
        port=settings.observability.http_port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=int(settings.app.shutdown_grace_seconds),
    )
    server = _ServerWithoutSignalHandlers(config)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for received in _SHUTDOWN_SIGNALS:
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(received, _on_signal, received, stopping)

    server_task = asyncio.create_task(server.serve(), name="operational-server")
    metrics_task = asyncio.create_task(application.refresh_pool_metrics(), name="pool-metrics")
    stop_task = asyncio.create_task(stopping.wait(), name="shutdown-signal")

    logger.info(
        "serving operational endpoints",
        host=settings.observability.http_host,
        port=settings.observability.http_port,
        health=settings.observability.health_path,
        ready=settings.observability.ready_path,
        metrics=settings.observability.metrics_path,
    )
    try:
        await asyncio.wait({server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        server.should_exit = True
        for task in (metrics_task, stop_task):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await metrics_task
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        for received in _SHUTDOWN_SIGNALS:
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(received)


def _on_signal(received: signal.Signals, stopping: asyncio.Event) -> None:
    get_logger("app.runtime").info("shutdown signal received", signal=received.name)
    stopping.set()


@contextlib.asynccontextmanager
async def running(settings: Settings, clock: Clock | None = None) -> AsyncIterator[Application]:
    """Start an application, yield it, and stop it again."""
    application = Application.build(settings, clock)
    configure_from_settings(settings)
    await application.start()
    try:
        yield application
    finally:
        await application.stop()


async def run(settings: Settings) -> int:
    """Run one process to completion, returning its exit code."""
    configure_from_settings(settings)
    logger = get_logger("app.runtime")
    application = Application.build(settings)
    try:
        await application.start()
    except Exception as exc:
        logger.critical(
            "startup failed", error=str(exc), error_type=type(exc).__name__, exc_info=True
        )
        await application.stop()
        return 1
    try:
        await serve(application)
    finally:
        await application.stop()
    return 0
