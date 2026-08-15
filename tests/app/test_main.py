"""Tests for the command line entry point and its exit codes.

Exit codes are an interface. A supervisor uses them to decide whether restarting will
help: 78 means the deployment is misconfigured and restarting will fail identically, so
back off and alert, while a crash means try again. Getting that wrong produces either a
restart loop against a bad config or a dead process nobody notices.

The process is launched as a real subprocess rather than by calling ``main`` in-process.
Nothing is patched, so what is measured is the exit code the operating system reports,
which is the thing a supervisor actually sees.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tradingsys.__main__ import EXIT_CONFIGURATION_ERROR
from tradingsys.app.runtime import run
from tradingsys.config import Environment, load_settings

REPO_ROOT = Path(__file__).resolve().parents[2]

# The application refuses to reach a database it cannot resolve, quickly.
UNREACHABLE_DATABASE = {
    "TRADINGSYS_DATABASE__HOST": "127.0.0.1",
    "TRADINGSYS_DATABASE__PORT": "1",
    "TRADINGSYS_DATABASE__CONNECT_TIMEOUT_SECONDS": "1",
}


def run_process(env: dict[str, str], timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
    """Launch the real entry point with an explicit environment."""
    return subprocess.run(
        [sys.executable, "-m", "tradingsys"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "TRADINGSYS_CONFIG_DIR": str(REPO_ROOT / "config"),
            "TRADINGSYS_APP__ENVIRONMENT": "test",
            **env,
        },
        timeout=timeout,
        check=False,
    )


class TestConfigurationFailure:
    def test_a_missing_credential_exits_with_the_configuration_code(self) -> None:
        result = run_process({})
        assert result.returncode == EXIT_CONFIGURATION_ERROR

    def test_the_code_is_the_documented_sysexits_value(self) -> None:
        # EX_CONFIG from sysexits.h. Chosen rather than invented so that supervisors
        # and shell conventions already understand it.
        assert EXIT_CONFIGURATION_ERROR == 78

    def test_the_reason_goes_to_stderr(self) -> None:
        result = run_process({})
        assert "configuration error" in result.stderr
        assert "database.password" in result.stderr

    def test_stdout_stays_clean_on_a_configuration_failure(self) -> None:
        # Nothing has been configured yet, including logging, so a diagnostic must not
        # be written to stdout where a log collector would try to parse it as JSON.
        assert run_process({}).stdout == ""

    def test_an_unrecognised_variable_also_aborts(self) -> None:
        result = run_process(
            {"TRADINGSYS_DATABASE__PASSWORD": "x", "TRADINGSYS_DATABASE__NONSENSE": "1"}
        )
        assert result.returncode == EXIT_CONFIGURATION_ERROR
        assert "nonsense" in result.stderr


class TestStartupFailure:
    def test_an_unreachable_database_exits_non_zero_but_not_as_a_config_error(self) -> None:
        # The configuration is valid here; the dependency is not. A supervisor should
        # retry this, which is why it must not share the configuration exit code.
        result = run_process({"TRADINGSYS_DATABASE__PASSWORD": "x", **UNREACHABLE_DATABASE})
        assert result.returncode != 0
        assert result.returncode != EXIT_CONFIGURATION_ERROR

    def test_the_failure_is_logged_before_exit(self) -> None:
        result = run_process({"TRADINGSYS_DATABASE__PASSWORD": "x", **UNREACHABLE_DATABASE})
        assert "startup failed" in result.stdout

    def test_the_log_line_is_structured_json(self) -> None:
        # Logging is configured before dependencies are touched, so even a startup
        # failure is machine readable.
        result = run_process({"TRADINGSYS_DATABASE__PASSWORD": "x", **UNREACHABLE_DATABASE})
        critical = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{") and '"critical"' in line
        ]
        assert critical
        assert critical[0]["event"] == "startup failed"
        assert critical[0]["environment"] == "test"

    def test_the_password_never_reaches_the_output(self) -> None:
        result = run_process(
            {"TRADINGSYS_DATABASE__PASSWORD": "unmistakable-secret", **UNREACHABLE_DATABASE}
        )
        assert "unmistakable-secret" not in result.stdout
        assert "unmistakable-secret" not in result.stderr


@pytest.mark.integration
class TestRunAgainstLiveDependencies:
    async def test_run_returns_one_when_startup_fails(self) -> None:
        # Exercises tradingsys.app.runtime.run directly, including its shutdown path,
        # rather than only the subprocess wrapper.
        settings = load_settings(
            config_dir=REPO_ROOT / "config",
            environment=Environment.TEST,
            environ={
                "TRADINGSYS_DATABASE__PASSWORD": "x",
                **UNREACHABLE_DATABASE,
            },
        )
        assert await run(settings) == 1
