"""Fixtures for tests that need a real PostgreSQL with TimescaleDB.

Integration tests connect to the database defined by the ``test`` configuration
environment, which matches the compose stack. They **fail** rather than skip when the
database is unreachable: a suite that quietly skips its only real storage coverage
reports green while testing nothing.

Run them through the one verification path::

    scripts/verify.sh

The settings themselves come from the shared ``live_settings`` fixture in the parent
conftest, so a missing credential is reported once rather than once per test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tradingsys.core.clock import SystemClock
from tradingsys.core.currency import default_registry
from tradingsys.persistence.audit import AuditLog
from tradingsys.persistence.database import Database
from tradingsys.persistence.repositories import InstrumentRepository, MarketDataRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tradingsys.config import Settings

REQUIRED_TABLES = ("instruments", "ohlcv_bars", "ticks", "audit_log")


@pytest.fixture
async def database(live_settings: Settings) -> AsyncIterator[Database]:
    """A connected pool against the test database.

    Fails with an actionable message if the database is not running or the migrations
    have not been applied, rather than skipping.
    """
    db = Database(live_settings.database)
    try:
        await db.connect()
    except Exception as exc:  # the message matters more than the type
        pytest.fail(
            f"integration tests need PostgreSQL at {live_settings.database.dsn()}: {exc}\n"
            f"Start it with: scripts/verify.sh"
        )
    try:
        missing = [
            table
            for table in REQUIRED_TABLES
            if not await db.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
        ]
        if missing:
            pytest.fail(
                f"the test database is missing {', '.join(missing)}. Apply the migrations "
                f"with: scripts/verify.sh, or directly with "
                f"TRADINGSYS_APP__ENVIRONMENT=test uv run alembic upgrade head"
            )
        await _truncate(db)
        yield db
    finally:
        await db.close()


async def _truncate(database: Database) -> None:
    """Empty every table before a test.

    audit_log rejects DELETE by trigger and TRUNCATE by privilege, so the trigger is
    dropped and recreated around the truncation. This is the one place allowed to do
    that, and it exists so each test starts from an empty chain.
    """
    async with database.transaction() as connection:
        await connection.execute("DROP TRIGGER IF EXISTS audit_log_is_append_only ON audit_log")
        await connection.execute("TRUNCATE audit_log")
        await connection.execute(
            "CREATE TRIGGER audit_log_is_append_only "
            "BEFORE UPDATE OR DELETE ON audit_log "
            "FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation()"
        )
        await connection.execute("TRUNCATE ticks, ohlcv_bars, instruments CASCADE")


@pytest.fixture
def audit_log(database: Database) -> AuditLog:
    return AuditLog(database, SystemClock())


@pytest.fixture
def instruments(database: Database) -> InstrumentRepository:
    return InstrumentRepository(database, default_registry)


@pytest.fixture
def market_data(database: Database) -> MarketDataRepository:
    return MarketDataRepository(database)
