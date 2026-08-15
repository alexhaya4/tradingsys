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
from tradingsys.core.errors import ConfigurationError
from tradingsys.observability.correlation import clear_correlation_id
from tradingsys.observability.logging import reset_logging

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


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
