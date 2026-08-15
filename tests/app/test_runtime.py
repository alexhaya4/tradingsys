"""Tests for process wiring and lifecycle.

The wiring tests need no services: `Application.build` deliberately does no I/O, which
is what lets the dependency graph be checked cheaply. The tests that need a real
database and Redis are marked integration.
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import pytest

from tradingsys import __version__
from tradingsys.app.runtime import REQUIRED_TABLES, Application, running, serve
from tradingsys.config import Environment, load_settings
from tradingsys.core.clock import FixedClock, SystemClock
from tradingsys.core.errors import TradingSysError
from tradingsys.observability.health import HealthStatus
from tradingsys.persistence.audit import AuditCategory

if TYPE_CHECKING:
    from tradingsys.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def build_settings(**env: str) -> Settings:
    return load_settings(
        config_dir=REPO_ROOT / "config",
        environment=Environment.TEST,
        environ={"TRADINGSYS_DATABASE__PASSWORD": "s3cret", **env},
    )


@pytest.fixture
def settings() -> Settings:
    return build_settings()


class TestWiring:
    def test_build_does_no_io(self, settings: Settings) -> None:
        # Nothing here connects: a misconfigured deployment must fail at start(), where
        # the error can be reported, not at import or construction time.
        application = Application.build(settings)
        assert not application.database.is_connected
        assert not application.is_started

    def test_components_share_one_database(self, settings: Settings) -> None:
        application = Application.build(settings)
        assert application.instruments is not None
        assert application.market_data is not None
        assert application.audit_log is not None

    def test_readiness_covers_every_external_dependency(self, settings: Settings) -> None:
        application = Application.build(settings)
        assert {check.name for check in application.readiness.checks} == {"database", "redis"}

    def test_the_readiness_timeout_comes_from_configuration(self, settings: Settings) -> None:
        application = Application.build(settings)
        assert (
            application.readiness.timeout_seconds
            == settings.observability.readiness_timeout_seconds
        )

    def test_metrics_carry_the_running_version(self, settings: Settings) -> None:
        application = Application.build(settings)
        rendered = application.metrics.render().decode()
        assert f'version="{__version__}"' in rendered
        assert 'environment="test"' in rendered

    def test_an_injected_clock_is_used(self, settings: Settings) -> None:
        clock = FixedClock(datetime(2025, 3, 5, 12, 0, tzinfo=UTC))
        application = Application.build(settings, clock)
        assert application.clock is clock

    def test_the_default_clock_is_the_system_clock(self, settings: Settings) -> None:
        assert isinstance(Application.build(settings).clock, SystemClock)

    def test_the_http_app_serves_the_configured_paths(self, settings: Settings) -> None:
        application = Application.build(settings)
        routes = {
            route.path  # type: ignore[attr-defined]
            for route in application.build_http_app().routes
        }
        assert routes == {
            settings.observability.health_path,
            settings.observability.ready_path,
            settings.observability.metrics_path,
        }

    def test_redis_credentials_come_from_settings(self) -> None:
        settings = build_settings(TRADINGSYS_REDIS__PASSWORD="redis-secret")
        application = Application.build(settings)
        assert application.redis.connection_pool.connection_kwargs["password"] == "redis-secret"


class TestStartupFailure:
    async def test_an_unreachable_database_aborts_startup(self) -> None:
        settings = build_settings(
            TRADINGSYS_DATABASE__HOST="127.0.0.1",
            TRADINGSYS_DATABASE__PORT="1",
            TRADINGSYS_DATABASE__CONNECT_TIMEOUT_SECONDS="1",
        )
        application = Application.build(settings)
        with pytest.raises(Exception, match="could not connect"):
            await application.start()
        assert not application.is_started
        await application.stop()

    async def test_stop_is_safe_before_start(self, settings: Settings) -> None:
        await Application.build(settings).stop()

    async def test_stop_is_idempotent(self, settings: Settings) -> None:
        application = Application.build(settings)
        await application.stop()
        await application.stop()


class TestRequiredTables:
    def test_the_list_matches_the_migration(self) -> None:
        migration = (REPO_ROOT / "migrations" / "versions" / "0001_initial_schema.py").read_text()
        for table in REQUIRED_TABLES:
            assert f"CREATE TABLE {table}" in migration


@pytest.mark.integration
class TestLifecycle:
    """End to end against the compose stack."""

    @pytest.fixture
    def live_settings(self) -> Settings:
        """Settings from the real process environment, so the password is the real one."""
        return load_settings(config_dir=REPO_ROOT / "config", environment=Environment.TEST)

    async def test_start_and_stop(self, live_settings: Settings) -> None:
        application = Application.build(live_settings)
        await application.start()
        while_running = (application.is_started, application.database.is_connected)
        await application.stop()
        after_stopping = (application.is_started, application.database.is_connected)
        assert while_running == (True, True)
        assert after_stopping == (False, False)

    async def test_startup_is_recorded_in_the_audit_log(self, live_settings: Settings) -> None:
        application = Application.build(live_settings)
        await application.start()
        try:
            head = await application.audit_log.head()
            assert head is not None
            assert head.category is AuditCategory.SYSTEM
            assert head.action == "started"
            assert head.payload["version"] == __version__
        finally:
            await application.stop()

    async def test_the_startup_entry_never_contains_a_secret(self, live_settings: Settings) -> None:
        application = Application.build(live_settings)
        await application.start()
        try:
            entries = await application.audit_log.read()
            rendered = repr([entry.payload for entry in entries])
            assert "s3cret" not in rendered
        finally:
            await application.stop()

    async def test_shutdown_is_recorded_and_chains_to_startup(
        self, live_settings: Settings
    ) -> None:
        # Sequence numbers are relative to whatever the log already holds, so this
        # reads from the head that existed before rather than assuming it starts at 1.
        probe = Application.build(live_settings)
        await probe.database.connect()
        existing = await probe.audit_log.head()
        await probe.database.close()
        first_new = 1 if existing is None else existing.sequence + 1

        application = Application.build(live_settings)
        await application.start()
        await application.stop()

        verifier = Application.build(live_settings)
        await verifier.start()
        try:
            entries = await verifier.audit_log.read(
                start_sequence=first_new, end_sequence=first_new + 1
            )
            assert [entry.action for entry in entries] == ["started", "stopped"]
            assert entries[1].links_to(entries[0])
            assert (await verifier.audit_log.verify()).is_intact
        finally:
            await verifier.stop()

    async def test_readiness_passes_against_live_dependencies(
        self, live_settings: Settings
    ) -> None:
        async with running(live_settings) as application:
            report = await application.readiness.evaluate()
            assert report.status is HealthStatus.PASS
            assert {check.name for check in report.checks} == {"database", "redis"}

    async def test_readiness_fails_once_the_database_closes(self, live_settings: Settings) -> None:
        application = Application.build(live_settings)
        await application.start()
        try:
            await application.database.close()
            report = await application.readiness.evaluate()
            assert report.status is HealthStatus.FAIL
            assert [check.name for check in report.failures] == ["database"]
        finally:
            await application.stop()

    async def test_an_unmigrated_database_is_reported_clearly(
        self, live_settings: Settings
    ) -> None:
        # Uses a scratch database rather than dropping a table from the shared one:
        # a test that damages the schema breaks every test that runs after it.
        scratch = "tradingsys_unmigrated"
        admin = await asyncpg.connect(
            dsn=live_settings.database.model_copy(update={"database": "postgres"}).dsn(
                reveal_password=True
            )
        )
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
            await admin.execute(f'CREATE DATABASE "{scratch}"')
        finally:
            await admin.close()

        empty = live_settings.model_copy(
            update={"database": live_settings.database.model_copy(update={"database": scratch})}
        )
        seeder = await asyncpg.connect(dsn=empty.database.dsn(reveal_password=True))
        try:
            await seeder.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        finally:
            await seeder.close()

        application = Application.build(empty)
        try:
            with pytest.raises(TradingSysError, match="alembic upgrade head"):
                await application.start()
        finally:
            await application.stop()
            admin = await asyncpg.connect(
                dsn=live_settings.database.model_copy(update={"database": "postgres"}).dsn(
                    reveal_password=True
                )
            )
            try:
                await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
            finally:
                await admin.close()

    async def test_the_pool_metric_refresher_stops_when_cancelled(
        self, live_settings: Settings
    ) -> None:
        async with running(live_settings) as application:
            task = asyncio.create_task(application.refresh_pool_metrics(interval_seconds=0.01))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert "tradingsys_db_pool_connections" in application.metrics.render().decode()

    async def test_serve_stops_on_a_shutdown_signal(self, live_settings: Settings) -> None:
        async with running(live_settings) as application:
            server = asyncio.create_task(serve(application))
            await asyncio.sleep(0.5)
            os.kill(os.getpid(), signal.SIGTERM)
            async with asyncio.timeout(10):
                await server
