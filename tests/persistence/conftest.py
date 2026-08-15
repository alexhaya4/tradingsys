"""Fixtures for tests that need a real PostgreSQL with TimescaleDB.

Integration tests connect to the database defined by the ``test`` configuration
environment, which matches the compose stack. They **fail** rather than skip when the
database is unreachable: a suite that quietly skips its only real storage coverage
reports green while testing nothing.

Run them with::

    docker compose up -d db
    uv run alembic upgrade head
    TRADINGSYS_APP__ENVIRONMENT=test uv run pytest -m integration
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tradingsys.config import Environment, load_settings
from tradingsys.core.clock import SystemClock
from tradingsys.core.currency import default_registry
from tradingsys.persistence.audit import AuditLog
from tradingsys.persistence.database import Database
from tradingsys.persistence.repositories import InstrumentRepository, MarketDataRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tradingsys.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_TABLES = ("instruments", "ohlcv_bars", "ticks", "audit_log")


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Test environment settings, with the database password from the environment."""
    return load_settings(
        config_dir=REPO_ROOT / "config",
        environment=Environment.TEST,
    )


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    """A connected pool against the test database.

    Fails with an actionable message if the database is not running or the migrations
    have not been applied, rather than skipping.
    """
    db = Database(settings.database)
    try:
        await db.connect()
    except Exception as exc:  # the message matters more than the type
        pytest.fail(
            f"integration tests need PostgreSQL at {settings.database.dsn()}: {exc}\n"
            f"Start it with: docker compose up -d db"
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
                f"with: TRADINGSYS_APP__ENVIRONMENT=test uv run alembic upgrade head"
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
