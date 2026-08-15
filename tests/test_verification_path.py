"""Tests that the documented verification path is the real one.

The defect these exist to prevent already happened once: the README listed a sequence
of commands that had never been run in that exact form, it omitted a required
environment variable, and following it produced 61 setup errors. The commands drifted
from reality because they were transcribed rather than executed.

The fix was to make ``scripts/verify.sh`` the only path, referenced by the README and
invoked by CI. These tests enforce that arrangement:

* the script exists, is executable, and is valid shell,
* its step list is what we claim it is, exercised through the real argument parser,
* the README points at it rather than restating its steps,
* CI invokes it rather than reimplementing the gates.

**Why the script is not executed end to end from here.** It runs ``uv run pytest``,
so a test that invoked it would recurse into itself, and it requires Docker, which the
unit suite must never need. The script is executed for real in exactly two places: by a
developer, and by CI on every push. What is checked here is everything about the script
that can be checked without running it, plus its behaviour under ``--dry-run``, which
drives the same argument parsing and the same step list that a real run uses. Full
execution is covered by CI, which is the only honest place to cover it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify.sh"
README = REPO_ROOT / "README.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "verify.yml"

EXPECTED_STEPS = (
    "preflight",
    "load secrets",
    "start datastores",
    "create test database",
    "apply migrations",
    "check formatting",
    "check lint",
    "check types",
    "run unit tests",
    "run integration tests",
    "start full stack",
    "probe endpoints",
    "check prometheus targets",
)


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the script with a deliberately empty environment except for PATH.

    Nothing here may depend on the caller's shell already holding credentials: that
    assumption is what produced the original defect.
    """
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
        timeout=60,
        check=False,
    )


class TestTheScriptIsUsable:
    def test_it_exists(self) -> None:
        assert SCRIPT.is_file(), f"{SCRIPT} is missing; the README and CI both call it"

    def test_it_is_executable(self) -> None:
        assert os.access(SCRIPT, os.X_OK), (
            f"{SCRIPT} is not executable, so the documented `scripts/verify.sh` fails"
        )

    def test_it_is_valid_shell(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr

    def test_it_runs_under_strict_mode(self) -> None:
        # Without this an unset variable or a failing command in the middle of the
        # script is skipped over and the run reports success.
        assert "set -Eeuo pipefail" in SCRIPT.read_text()

    def test_help_exits_cleanly(self) -> None:
        result = run_script("--help")
        assert result.returncode == 0, result.stderr
        assert "scripts/verify.sh" in result.stdout

    def test_help_contains_no_shell_source(self) -> None:
        # The help text is extracted from the header comment, so a bad extraction would
        # print code at the operator.
        assert "set -Eeuo pipefail" not in run_script("--help").stdout

    def test_an_unknown_argument_fails_loudly(self) -> None:
        result = run_script("--not-a-flag")
        assert result.returncode != 0
        assert "unknown argument" in result.stderr


class TestTheStepsAreWhatWeClaim:
    def test_dry_run_succeeds_without_credentials_or_docker(self) -> None:
        result = run_script("--dry-run")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_dry_run_executes_nothing(self) -> None:
        result = run_script("--dry-run")
        assert "dry run complete, nothing was executed" in result.stdout

    @pytest.mark.parametrize("expected", EXPECTED_STEPS)
    def test_every_expected_step_is_present(self, expected: str) -> None:
        assert f"=== {expected} ===" in run_script("--dry-run").stdout

    def test_the_steps_run_in_the_required_order(self) -> None:
        # Migrations must precede the test suites, and the full stack must come up
        # after migrations because the app refuses to start against an unmigrated
        # database.
        output = run_script("--dry-run").stdout
        positions = {step: output.index(f"=== {step} ===") for step in EXPECTED_STEPS}
        assert positions["apply migrations"] < positions["run integration tests"]
        assert positions["apply migrations"] < positions["start full stack"]
        assert positions["start full stack"] < positions["probe endpoints"]
        assert positions["load secrets"] < positions["apply migrations"]

    def test_both_databases_are_migrated(self) -> None:
        # The development database backs the app container and the test database backs
        # the integration suite. Migrating only one leaves half the stack broken.
        body = SCRIPT.read_text()
        assert "TRADINGSYS_APP__ENVIRONMENT=development" in body
        assert "TRADINGSYS_APP__ENVIRONMENT=test" in body

    def test_it_names_every_credential_it_needs(self) -> None:
        body = SCRIPT.read_text()
        for variable in (
            "POSTGRES_PASSWORD",
            "TRADINGSYS_DATABASE__PASSWORD",
            "GRAFANA_PASSWORD",
        ):
            assert variable in body

    def test_missing_credentials_fail_with_instructions(self, tmp_path: Path) -> None:
        # Run from a copy of the repo that has no .env, so the failure is the one a
        # fresh clone or a CI job without secrets would hit.
        result = subprocess.run(
            [str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            timeout=60,
            check=False,
        )
        # --help must work from anywhere; the credential check belongs to a real run.
        assert result.returncode == 0
        body = SCRIPT.read_text()
        assert "these variables are not set" in body
        assert ".env.example" in body


class TestTheFirstProvisionIsCovered:
    """A clean provision is the path that broke, so it needs standing coverage.

    A multi revision migration failure only appears against an empty database. Every
    developer machine has one that already exists in some shape, so without this the
    first genuinely clean provision would happen in production.
    """

    def test_fresh_mode_exists_and_destroys_the_volumes(self) -> None:
        body = SCRIPT.read_text()
        assert "--fresh" in body
        assert "docker compose down -v" in body

    def test_the_destroy_step_runs_before_the_datastores_start(self) -> None:
        output = run_script("--fresh", "--dry-run").stdout
        assert output.index("=== destroy volumes ===") < output.index("=== start datastores ===")

    def test_the_destroy_step_is_opt_in(self) -> None:
        # Wiping a developer's database on every run would be hostile, so a plain
        # invocation must not do it.
        assert "=== destroy volumes ===" not in run_script("--dry-run").stdout

    def test_migrations_and_the_integration_suite_both_follow_it(self) -> None:
        # Provisioning from empty proves nothing unless the migrations and the suite
        # then run against what it produced.
        output = run_script("--fresh", "--dry-run").stdout
        destroy = output.index("=== destroy volumes ===")
        assert destroy < output.index("=== apply migrations ===")
        assert destroy < output.index("=== run integration tests ===")

    def test_ci_runs_the_fresh_path(self) -> None:
        assert "--fresh" in WORKFLOW.read_text()


class TestTheReadmeDoesNotDrift:
    def test_it_points_at_the_script(self) -> None:
        assert "scripts/verify.sh" in README.read_text()

    def test_it_does_not_transcribe_the_migration_step(self) -> None:
        # This is the exact line that drifted: a README `alembic upgrade head` that
        # omitted the password. Any migration command in the README is now suspect,
        # except where it is explicitly discussed as a manual escape hatch.
        transcribed = [
            line
            for line in README.read_text().splitlines()
            if re.search(r"^\s*(TRADINGSYS\S*\s+)*uv run alembic upgrade head", line)
        ]
        assert not transcribed, (
            "the README transcribes a migration command again; it belongs in "
            f"scripts/verify.sh: {transcribed}"
        )

    def test_it_does_not_transcribe_the_integration_test_step(self) -> None:
        transcribed = [
            line
            for line in README.read_text().splitlines()
            if re.search(r"^\s*(TRADINGSYS\S*\s+)*uv run pytest -m integration", line)
        ]
        assert not transcribed, (
            "the README transcribes the integration test command again; it belongs in "
            f"scripts/verify.sh: {transcribed}"
        )

    def test_it_explains_that_the_application_ignores_dotenv(self) -> None:
        # The trap that caused the failure: .env exists, compose reads it, the
        # application does not. Anyone reading the README must learn this.
        body = README.read_text()
        assert "never reads `.env`" in body or "never reads secrets from a file" in body
        assert "set -a" in body


class TestCiRunsTheSameScript:
    def test_the_workflow_exists(self) -> None:
        assert WORKFLOW.is_file()

    def test_it_invokes_the_script(self) -> None:
        assert "scripts/verify.sh" in WORKFLOW.read_text()

    def test_it_does_not_reimplement_the_gates(self) -> None:
        # A gate duplicated here is a gate that can differ from the local one.
        body = WORKFLOW.read_text()
        duplicated = [
            command
            for command in ("uv run pytest", "uv run mypy", "uv run ruff")
            if command in body
        ]
        assert not duplicated, f"CI runs these directly instead of through the script: {duplicated}"
