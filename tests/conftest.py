"""Test-wide fixtures.

Context variables outlive an individual test when they are set without a scope, which
the correlation module deliberately allows. Clearing between tests keeps one test's
binding from appearing in another's assertions.

The ``live_settings`` fixture lives here rather than in each suite because both the
persistence and the app integration tests need the same thing: settings for the test
environment, resolved from the real process environment so that the database password
is the real one. Defining it once means a missing credential is reported once, in one
voice, rather than as one traceback per test.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from tradingsys.config import Environment, Settings, load_settings
from tradingsys.config.loader import missing_variables
from tradingsys.core.clock import SystemClock
from tradingsys.core.currency import default_registry
from tradingsys.core.errors import ConfigurationError
from tradingsys.observability.correlation import clear_correlation_id
from tradingsys.observability.logging import reset_logging
from tradingsys.persistence.audit import AuditLog
from tradingsys.persistence.database import Database
from tradingsys.persistence.repositories import InstrumentRepository, MarketDataRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


REQUIRED_TABLES = ("instruments", "ohlcv_bars", "ticks", "audit_log")
"""Tables the integration suite cannot run without."""


@pytest.fixture(autouse=True)
def isolated_context() -> Iterator[None]:
    """Start and end every test with no correlation ID and no logging configuration."""
    clear_correlation_id()
    yield
    clear_correlation_id()
    reset_logging()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Report an unusable configuration once, before any integration test runs.

    Runs last so that marker based deselection has already happened: ``items`` must be
    what will actually run, not everything that was collected, or a ``-m integration``
    invocation still looks like a mixed selection.

    A missing credential is a precondition of the whole integration suite rather than
    a property of any one test. Left to the fixture, it is reported once per selected
    test: the original occurrence produced 61 near-identical tracebacks totalling
    13,000 lines, in which the one actionable line appeared 61 times.

    When every selected test needs live settings, the session stops with a single
    message. When the selection is mixed, it does not, because aborting would throw
    away unit results that are still worth having; those runs fall back to the
    fixture's per-test failure.
    """
    integration = [item for item in items if item.get_closest_marker("integration")]
    if not integration:
        return
    try:
        load_settings(config_dir=CONFIG_DIR, environment=Environment.TEST)
    except ConfigurationError as exc:
        if len(integration) == len(items):
            pytest.exit(_configuration_help(exc), returncode=1)


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    """Settings for the test environment, from the real process environment.

    The failure is raised outside the ``except`` block so that Python does not chain
    it onto the pydantic ValidationError. Chained, the actionable message is printed
    below twenty lines of library traceback; unchained, it is the whole output.
    """
    try:
        return load_settings(config_dir=CONFIG_DIR, environment=Environment.TEST)
    except ConfigurationError as exc:
        message = _configuration_help(exc)
    pytest.fail(message, pytrace=False)


def _configuration_help(error: ConfigurationError) -> str:
    """Turn a startup configuration failure into instructions an operator can act on.

    The per-field remedy comes from the loader, which knows whether each field is
    missing or merely unrecognised. Only the genuinely missing ones get a "set this"
    instruction: naming a variable that is already set sends the reader hunting for
    the wrong problem.
    """
    lines = ["integration tests could not load their configuration.", "", str(error), ""]
    cause = error.__cause__
    absent = missing_variables(cause) if isinstance(cause, ValidationError) else ()
    if absent:
        lines += [
            f"Set {', '.join(absent)} before running the suite.",
            "The application never reads secrets from a configuration file, so they",
            "come from the environment:",
            "",
            "    set -a; . ./.env; set +a",
            "    uv run pytest -m integration",
            "",
            "Or run the whole verification path, which does this for you:",
            "",
            "    scripts/verify.sh",
        ]
    else:
        lines += [
            "Every required value is present, so this is not a missing credential.",
            "Resolve the fields listed above, then run:",
            "",
            "    scripts/verify.sh",
        ]
    return "\n".join(lines)


# Moved here from tests/persistence/conftest.py on 2026-08-19. The app integration tests
# need the same connected database and repositories, and this file's own docstring
# already anticipated that: defining them once means a missing credential is reported in
# one voice rather than once per suite.
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
