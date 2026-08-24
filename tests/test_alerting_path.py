"""Tests for the alerting path: the scripts, their wiring, and their delivery.

**Why this file exists at all.** The alerting path is the one component that cannot
verify itself. Everything else on the host asserts something and reports through it;
it has nothing to report through, so its real verification is a message arriving on a
phone, which no test can perform. What a test can do is establish everything up to that
last hop: that the scripts run, that the wiring points at files which exist, that a
delivery failure is loud, that a repeat is suppressed, and that a recovery is sent.

**The delivery tests are real HTTP against a real server.** notify.sh takes its API base
from the environment, so the suite points it at a local server and exercises the actual
curl invocation, the actual response handling, and the actual exit codes. Nothing here
stands in for the script.

**What is deliberately not covered.** Whether Telegram delivers to the handset, and
whether the dead man switch notices an absence. Both are properties of services outside
this repository, and both are covered by the canary and by the operator seeing it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import pytest

from tests.factories import eurusd
from tradingsys.core.provenance import TickSource
from tradingsys.venues.models import Quote

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tradingsys.persistence.database import Database
    from tradingsys.persistence.repositories import InstrumentRepository, MarketDataRepository

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVISION = REPO_ROOT / "deploy" / "provision"

NOTIFY = PROVISION / "notify.sh"
ALERT = PROVISION / "alert.sh"
CANARY = PROVISION / "canary.sh"
ALERT_UNIT_FAILED = PROVISION / "alert_unit_failed.sh"
BOOTSTRAP = PROVISION / "bootstrap.sh"

SHELL_SCRIPTS = tuple(sorted(PROVISION.glob("*.sh")))
UNIT_FILES = tuple(sorted([*PROVISION.glob("*.service"), *PROVISION.glob("*.timer")]))

TOKEN = "12345:test-token"
CHAT_ID = "-1001234567890"

# A host that cannot resolve, so curl fails at the transport without retrying. Connection
# refused would be retried by --retry-connrefused and turn a fast test into a slow one.
UNRESOLVABLE = "http://tradingsys-alerting.invalid"


@dataclass
class Received:
    """One request the stub server answered."""

    path: str
    body: str

    def form(self) -> dict[str, list[str]]:
        return parse_qs(self.body)

    def field(self, name: str) -> str:
        values = self.form().get(name, [])
        assert values, f"{name} absent from {self.body!r}"
        return values[0]


@dataclass
class StubEndpoint:
    """A local HTTP server standing in for Telegram or for the dead man switch.

    It is not a mock of the alerting path. It is the far end of it, which is a real
    server in production too, and it answers over real HTTP on a real socket.
    """

    status: int = 200
    payload: str = '{"ok": true}'
    received: list[Received] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        # server_address is typed as a generic address tuple, so the host arrives as
        # bytes on some platforms and str here. Normalised rather than formatted blind.
        text_host = host.decode() if isinstance(host, bytes) else str(host)
        return f"http://{text_host}:{port}"

    def start(self) -> None:
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _record_and_answer(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode() if length else ""
                endpoint.received.append(Received(path=self.path, body=body))
                encoded = endpoint.payload.encode()
                self.send_response(endpoint.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self) -> None:  # the stdlib names this hook, not us
                """Telegram is a POST; the dead man switch is a GET."""
                self._record_and_answer()

            def do_GET(self) -> None:
                self._record_and_answer()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                """Silence the stdlib's stderr logging so a passing run is quiet."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.fixture
def telegram() -> Iterator[StubEndpoint]:
    endpoint = StubEndpoint()
    endpoint.start()
    yield endpoint
    endpoint.stop()


@pytest.fixture
def deadman() -> Iterator[StubEndpoint]:
    endpoint = StubEndpoint()
    endpoint.start()
    yield endpoint
    endpoint.stop()


def script_env(
    *,
    api: str | None = None,
    state_dir: Path | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """A deliberately minimal environment, as a systemd unit would supply."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "TRADINGSYS_ALERT_TELEGRAM_TOKEN": TOKEN,
        "TRADINGSYS_ALERT_TELEGRAM_CHAT_ID": CHAT_ID,
    }
    if api is not None:
        env["TRADINGSYS_ALERT_TELEGRAM_API"] = api
    if state_dir is not None:
        env["TRADINGSYS_ALERT_STATE_DIR"] = str(state_dir)
    if extra:
        env.update(extra)
    return env


def executable_lines(path: Path) -> list[str]:
    """The lines of a script or compose file with comments removed.

    Every rule below matches text, and the text these files carry includes long comments
    describing the defects the rules exist to prevent. Matching those is a check firing on
    its own documentation.
    """
    return [line for line in path.read_text().splitlines() if not line.strip().startswith("#")]


def run(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=60,
        check=False,
    )


class TestTheScriptsAreRunnable:
    @pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
    def test_it_is_executable(self, script: Path) -> None:
        assert os.access(script, os.X_OK), (
            f"{script.name} is not executable, so the systemd unit that names it fails"
        )

    @pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
    def test_it_is_valid_shell(self, script: Path) -> None:
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
    def test_it_handles_unset_variables_strictly(self, script: Path) -> None:
        # -e is deliberately absent from the assertion scripts, which collect several
        # failures before reporting them. -u is not optional anywhere: an unset variable
        # silently becoming an empty string is how a check asserts nothing.
        assert re.search(r"^set -[A-Za-z]*u", script.read_text(), re.MULTILINE), (
            f"{script.name} does not set -u"
        )


class TestDelivery:
    """notify.sh against a server that answers."""

    def test_it_posts_the_subject_and_the_body(self, telegram: StubEndpoint) -> None:
        result = run(NOTIFY, "a subject", "a body", env=script_env(api=telegram.url))
        assert result.returncode == 0, result.stderr
        assert len(telegram.received) == 1
        sent = telegram.received[0]
        assert sent.path == f"/bot{TOKEN}/sendMessage"
        assert sent.field("chat_id") == CHAT_ID
        text = sent.field("text")
        assert "a subject" in text
        assert "a body" in text

    def test_the_message_says_which_host_and_when(self, telegram: StubEndpoint) -> None:
        # An alert that does not name its origin is one you have to go and correlate.
        run(NOTIFY, "subject", env=script_env(api=telegram.url))
        text = telegram.received[0].field("text")
        assert "host: " in text
        assert re.search(r"time: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text)

    def test_a_long_body_is_truncated_rather_than_rejected(self, telegram: StubEndpoint) -> None:
        # Telegram rejects a message over 4096 characters outright, which would lose the
        # subject along with the overflow.
        result = run(NOTIFY, "subject", "x" * 9000, env=script_env(api=telegram.url))
        assert result.returncode == 0, result.stderr
        assert len(telegram.received[0].field("text")) <= 3800

    def test_an_http_refusal_fails_loudly(self, telegram: StubEndpoint) -> None:
        telegram.status = 401
        telegram.payload = json.dumps({"ok": False, "description": "Unauthorized"})
        result = run(NOTIFY, "subject", env=script_env(api=telegram.url))
        assert result.returncode != 0
        assert "HTTP 401" in result.stderr
        assert "UNDELIVERED SUBJECT: subject" in result.stderr

    def test_a_transport_failure_names_curls_own_status(self) -> None:
        result = run(NOTIFY, "subject", env=script_env(api=UNRESOLVABLE))
        assert result.returncode != 0
        assert "curl exited" in result.stderr
        assert "UNDELIVERED SUBJECT: subject" in result.stderr

    def test_missing_configuration_names_both_variables(self) -> None:
        env = {"PATH": os.environ.get("PATH", "")}
        result = run(NOTIFY, "subject", "body", env=env)
        assert result.returncode != 0
        assert "TRADINGSYS_ALERT_TELEGRAM_TOKEN" in result.stderr
        assert "TRADINGSYS_ALERT_TELEGRAM_CHAT_ID" in result.stderr
        # The undelivered condition must still be readable in the journal.
        assert "SUBJECT: subject" in result.stderr
        assert "BODY: body" in result.stderr

    def test_no_argument_is_a_usage_error(self) -> None:
        result = run(NOTIFY, env=script_env())
        assert result.returncode == 2
        assert "usage" in result.stderr


class TestSuppression:
    """alert.sh, which owns whether a condition may speak again."""

    def test_the_first_raise_sends(self, telegram: StubEndpoint, tmp_path: Path) -> None:
        env = script_env(api=telegram.url, state_dir=tmp_path)
        result = run(ALERT, "raise", "unit:x.service", "x failed", "detail", env=env)
        assert result.returncode == 0, result.stderr
        assert len(telegram.received) == 1
        assert "x failed" in telegram.received[0].field("text")

    def test_a_repeat_inside_the_cooldown_is_suppressed(
        self, telegram: StubEndpoint, tmp_path: Path
    ) -> None:
        env = script_env(api=telegram.url, state_dir=tmp_path)
        for _ in range(5):
            run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        assert len(telegram.received) == 1, (
            "the health assertion runs every minute; without suppression a four hour "
            "outage delivers 240 messages and the channel gets muted"
        )

    def test_a_suppressed_raise_says_when_it_will_speak_again(
        self, telegram: StubEndpoint, tmp_path: Path
    ) -> None:
        env = script_env(api=telegram.url, state_dir=tmp_path)
        run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        result = run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        assert result.returncode == 0
        assert "suppressed" in result.stdout
        assert "next repeat due" in result.stdout

    def test_a_repeat_after_the_cooldown_says_it_is_still_failing(
        self, telegram: StubEndpoint, tmp_path: Path
    ) -> None:
        env = script_env(
            api=telegram.url, state_dir=tmp_path, extra={"TRADINGSYS_ALERT_REPEAT_MINUTES": "0"}
        )
        run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        assert len(telegram.received) == 2
        second = telegram.received[1].field("text")
        assert "STILL FAILING" in second
        assert "alert 2" in second

    def test_a_failed_delivery_is_retried_rather_than_waiting_out_the_cooldown(
        self, telegram: StubEndpoint, tmp_path: Path
    ) -> None:
        # The case that would lose the first alert of an outage: Telegram refuses, and a
        # naive implementation records the condition as reported and then says nothing
        # for thirty minutes.
        telegram.status = 401
        env = script_env(api=telegram.url, state_dir=tmp_path)
        first = run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        assert first.returncode != 0
        telegram.status = 200
        second = run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        assert second.returncode == 0, second.stderr
        assert len(telegram.received) == 2

    def test_a_broken_state_file_still_alerts(self, telegram: StubEndpoint, tmp_path: Path) -> None:
        env = script_env(api=telegram.url, state_dir=tmp_path)
        run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        state = next((tmp_path / "alerts").iterdir())
        state.write_text("garbage\n")
        result = run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        assert result.returncode == 0, result.stderr
        assert len(telegram.received) == 2, "unparseable state must err towards delivering"

    def test_an_unwritable_state_directory_still_alerts(
        self, telegram: StubEndpoint, tmp_path: Path
    ) -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("this is a file, so a directory cannot be created here")
        env = script_env(api=telegram.url, state_dir=blocked)
        result = run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        assert len(telegram.received) == 1, "losing state must not lose the alert"
        assert result.returncode != 0, "and the operator must be told suppression is off"
        assert "not writable" in result.stderr


class TestRecovery:
    def test_clearing_something_that_never_fired_sends_nothing(
        self, telegram: StubEndpoint, tmp_path: Path
    ) -> None:
        env = script_env(api=telegram.url, state_dir=tmp_path)
        result = run(ALERT, "clear", "unit:x.service", env=env)
        assert result.returncode == 0
        assert telegram.received == [], (
            "the healthy path runs every minute and must be silent, or the recovery "
            "message becomes the noise"
        )

    def test_clearing_a_firing_condition_reports_the_recovery(
        self, telegram: StubEndpoint, tmp_path: Path
    ) -> None:
        env = script_env(api=telegram.url, state_dir=tmp_path)
        run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        result = run(ALERT, "clear", "unit:x.service", "x is healthy again", env=env)
        assert result.returncode == 0, result.stderr
        text = telegram.received[1].field("text")
        assert "RECOVERED" in text
        assert "x is healthy again" in text
        assert "was failing for" in text

    def test_a_recovery_is_sent_once(self, telegram: StubEndpoint, tmp_path: Path) -> None:
        env = script_env(api=telegram.url, state_dir=tmp_path)
        run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        for _ in range(3):
            run(ALERT, "clear", "unit:x.service", env=env)
        assert len(telegram.received) == 2

    def test_an_undelivered_recovery_is_retried(
        self, telegram: StubEndpoint, tmp_path: Path
    ) -> None:
        env = script_env(api=telegram.url, state_dir=tmp_path)
        run(ALERT, "raise", "unit:x.service", "x failed", env=env)
        telegram.status = 401
        failed = run(ALERT, "clear", "unit:x.service", env=env)
        assert failed.returncode != 0
        telegram.status = 200
        retried = run(ALERT, "clear", "unit:x.service", env=env)
        assert retried.returncode == 0, retried.stderr
        assert "RECOVERED" in telegram.received[-1].field("text")


class TestTheCanary:
    def test_it_sends_and_then_pings(
        self, telegram: StubEndpoint, deadman: StubEndpoint, tmp_path: Path
    ) -> None:
        env = script_env(
            api=telegram.url,
            state_dir=tmp_path,
            extra={"TRADINGSYS_ALERT_DEADMAN_URL": f"{deadman.url}/ping/abc"},
        )
        result = run(CANARY, env=env)
        assert result.returncode == 0, result.stderr
        assert len(telegram.received) == 1
        assert len(deadman.received) == 1
        assert deadman.received[0].path == "/ping/abc"

    def test_the_message_carries_a_sequence_that_advances(
        self, telegram: StubEndpoint, deadman: StubEndpoint, tmp_path: Path
    ) -> None:
        # A sequence number is what makes one missing canary visible in the series to a
        # human reading the channel, independently of the dead man switch.
        env = script_env(
            api=telegram.url,
            state_dir=tmp_path,
            extra={"TRADINGSYS_ALERT_DEADMAN_URL": deadman.url},
        )
        run(CANARY, env=env)
        run(CANARY, env=env)
        assert "canary 1" in telegram.received[0].field("text")
        assert "canary 2" in telegram.received[1].field("text")

    def test_a_failed_delivery_does_not_ping(
        self, telegram: StubEndpoint, deadman: StubEndpoint, tmp_path: Path
    ) -> None:
        # The property the whole design rests on. If a canary that was never delivered
        # pinged anyway, the dead man switch would report everything as fine for exactly
        # as long as the alerting path was broken.
        telegram.status = 401
        env = script_env(
            api=telegram.url,
            state_dir=tmp_path,
            extra={"TRADINGSYS_ALERT_DEADMAN_URL": deadman.url},
        )
        result = run(CANARY, env=env)
        assert result.returncode != 0
        assert deadman.received == []
        assert "deliberately not been pinged" in result.stderr

    def test_an_unconfigured_switch_delivers_and_then_complains(
        self, telegram: StubEndpoint, tmp_path: Path
    ) -> None:
        env = script_env(api=telegram.url, state_dir=tmp_path)
        result = run(CANARY, env=env)
        assert len(telegram.received) == 1, "proving the path still matters"
        assert result.returncode != 0
        assert "TRADINGSYS_ALERT_DEADMAN_URL" in result.stderr

    def test_a_refused_ping_is_reported(
        self, telegram: StubEndpoint, deadman: StubEndpoint, tmp_path: Path
    ) -> None:
        deadman.status = 404
        env = script_env(
            api=telegram.url,
            state_dir=tmp_path,
            extra={"TRADINGSYS_ALERT_DEADMAN_URL": deadman.url},
        )
        result = run(CANARY, env=env)
        assert result.returncode != 0
        assert "HTTP 404" in result.stderr


class TestTheWiring:
    """The failure this class exists for: a component that exists and is reached by
    nothing. It has happened five times in this repository, most recently to notify.sh
    itself, which was written, was correct, and was neither committed nor called."""

    @pytest.mark.parametrize("unit", UNIT_FILES, ids=lambda p: p.name)
    def test_every_exec_line_names_a_file_that_exists(self, unit: Path) -> None:
        for line in unit.read_text().splitlines():
            match = re.match(r"^Exec(?:Start|Stop|StartPost)=(\S+)", line)
            if not match:
                continue
            command = match.group(1).replace("__REPO_ROOT__", str(REPO_ROOT))
            if not command.startswith("/") or command.startswith("/usr/bin/docker"):
                continue
            path = Path(command)
            assert path.is_file(), f"{unit.name} runs {command}, which does not exist"
            assert os.access(path, os.X_OK), f"{unit.name} runs {command}, which is not executable"

    @pytest.mark.parametrize(
        "unit",
        [PROVISION / "tradingsys-health.service", PROVISION / "tradingsys-recording.service"],
        ids=lambda p: p.name,
    )
    def test_every_assertion_delivers_its_failure(self, unit: Path) -> None:
        body = unit.read_text()
        assert "OnFailure=tradingsys-alert@%n.service" in body, (
            f"{unit.name} asserts something and cannot tell anyone when the assertion "
            f"fails, which is the state this host was in for four hours on 2026-08-19"
        )
        assert "Environment=TRADINGSYS_ALERT_KEY=%n" in body, (
            f"{unit.name} cannot clear its own alert, so a resolved condition would "
            f"stay open until someone looked"
        )
        assert "EnvironmentFile=__REPO_ROOT__/.env" in body

    def test_the_canary_does_not_alert_on_its_own_failure(self) -> None:
        # It would be sending down the path it has just failed to reach. Matched at the
        # start of a line so that the comment saying why it is absent does not satisfy
        # the check that it is absent.
        body = (PROVISION / "tradingsys-canary.service").read_text()
        assert not re.search(r"^OnFailure=", body, re.MULTILINE)

    def test_the_alert_template_is_instantiated_by_the_unit_name(self) -> None:
        body = (PROVISION / "tradingsys-alert@.service").read_text()
        assert "ExecStart=__REPO_ROOT__/deploy/provision/alert_unit_failed.sh %i" in body

    @pytest.mark.parametrize("unit", UNIT_FILES, ids=lambda p: p.name)
    def test_bootstrap_installs_every_unit_in_this_directory(self, unit: Path) -> None:
        # Asserted through the glob rather than through a list, because a list here is a
        # second definition of what the deployment consists of and the way it fails is
        # that a unit is written, wired, and never installed.
        body = BOOTSTRAP.read_text()
        assert '"${REPO_ROOT}"/deploy/provision/*.service' in body
        assert '"${REPO_ROOT}"/deploy/provision/*.timer' in body
        # Naming a unit to enable it is fine; naming one to install it is the list this
        # rule exists to prevent, so the installing command may not carry a unit name.
        installers = [
            line
            for line in body.splitlines()
            if unit.name in line and re.search(r"\b(tee|install)\b", line)
        ]
        assert not installers, (
            f"bootstrap.sh installs {unit.name} by name. It installs the directory, so a "
            f"name here is a second definition that can fall out of step with it: "
            f"{installers}"
        )

    def test_bootstrap_requires_the_alerting_secrets(self) -> None:
        body = BOOTSTRAP.read_text()
        for variable in (
            "TRADINGSYS_ALERT_TELEGRAM_TOKEN",
            "TRADINGSYS_ALERT_TELEGRAM_CHAT_ID",
            "TRADINGSYS_ALERT_DEADMAN_URL",
        ):
            assert variable in body, (
                f"{variable} is not checked by bootstrap, so a host can be provisioned "
                f"that records and cannot report that it stopped"
            )

    def test_the_example_environment_documents_every_variable_the_scripts_read(self) -> None:
        # A variable a script reads and no example mentions is one an operator learns
        # about from a failure.
        example = (REPO_ROOT / ".env.example").read_text()
        declared = set()
        for script in SHELL_SCRIPTS:
            declared |= set(re.findall(r"\$\{(TRADINGSYS_ALERT_[A-Z_]+)", script.read_text()))
        # Supplied by the systemd unit rather than by the operator: it is the unit's own
        # name, and an operator who set it by hand would file alerts under the wrong key.
        declared.discard("TRADINGSYS_ALERT_KEY")
        missing = sorted(name for name in declared if name not in example)
        assert not missing, f".env.example does not mention {missing}"


class TestTheSchedules:
    def test_the_canary_is_daily_until_the_run_completes(self) -> None:
        body = (PROVISION / "tradingsys-canary.timer").read_text()
        assert "OnCalendar=*-*-* 06:00:00" in body
        assert "Persistent=true" in body, (
            "a host that was down at the canary hour must send late rather than never, "
            "or the dead man switch reports an absence that has already been explained"
        )

    def test_the_weekly_switch_is_written_down_rather_than_remembered(self) -> None:
        # The director's amendment: daily until the 72 hour run completes, then weekly.
        # Both halves of that change have to happen together, so the file that carries
        # one names the other.
        body = (PROVISION / "tradingsys-canary.timer").read_text()
        assert "weekly" in body
        assert "grace" in body

    def test_the_recording_assertion_runs_on_its_own_threshold(self) -> None:
        timer = (PROVISION / "tradingsys-recording.timer").read_text()
        assert "OnUnitActiveSec=5min" in timer


class TestTheProvisioningScriptAsksRatherThanAsserts:
    """The uid the data directory must be owned by is derived, never written down.

    The comment said 999 and the image runs as 70. The assumption and the code
    implementing it were the same idea written twice, so no test could have caught the
    disagreement, and the only witness was a database that reported healthy and could not
    open its files. Hardcoding 70 instead would fail identically at the next image change.
    """

    def test_the_image_is_read_from_the_compose_definition(self) -> None:
        body = BOOTSTRAP.read_text()
        assert "config --format json" in body, (
            "the database image must come from the compose files, which already pin the "
            "tag, rather than being named a second time here"
        )
        assert 'services"]["db"]["image"' in body

    def test_the_uid_comes_from_the_image(self) -> None:
        body = BOOTSTRAP.read_text()
        assert 'id "$DB_IMAGE" -u postgres' in body
        assert 'id "$DB_IMAGE" -g postgres' in body

    def test_no_uid_is_written_into_the_script(self) -> None:
        # The specific numbers that have been wrong or would be next: 999 was asserted in
        # a comment and was wrong, 70 is right today and is exactly as unverifiable.
        #
        # Comments are stripped before matching. The first version of this test failed on
        # the comment explaining the defect, which is a check firing on its own
        # documentation, and a check that cannot tell prose from code is one nobody keeps.
        chowns = [
            line
            for line in executable_lines(BOOTSTRAP)
            if "chown" in line and re.search(r"\b(999|70)\b", line)
        ]
        assert not chowns, f"a uid is hardcoded in a chown again: {chowns}"

    def test_an_existing_cluster_is_verified_rather_than_chowned(self) -> None:
        # Chowning under a live postmaster is not a repair, it is the fault: open files
        # keep working while every new backend fails.
        body = BOOTSTRAP.read_text()
        assert "PG_VERSION" in body, "an existing cluster has to be recognised as one"
        assert "refuses rather than fixing it" in body

    def test_the_refusal_names_the_repair(self) -> None:
        # The path is the one it was asked about, so an operator can paste the line
        # rather than translating it.
        body = BOOTSTRAP.read_text()
        assert "chown -R ${PG_UID}:${PG_GID} ${pgdata}" in body
        assert "systemctl stop tradingsys.service" in body

    def test_the_decision_can_be_exercised_without_a_host(self) -> None:
        # Read only, no sudo, no mount, and ahead of the mount check so that a scratch
        # directory is enough. Without this the branch that refuses could only ever be
        # observed on the machine it was protecting.
        body = BOOTSTRAP.read_text()
        assert "--check-data-root" in body
        assert 'if [[ -n "$CHECK_DATA_ROOT" ]]; then' in body
        assert body.index('if [[ -n "$CHECK_DATA_ROOT" ]]; then') < body.index(
            'step "adopting the volume the platform mounted"'
        ), "the check mode must not require a mounted volume to reach"


class TestTheDatabaseHealthcheckAssertsServing:
    """It reported healthy for hours while every connection failed.

    pg_isready proves the postmaster answered a connection attempt, and that is true of a
    server whose backends cannot start. Everything downstream reads this one result:
    compose's depends_on, the systemd unit's up -d --wait, and assert_healthy.sh.
    """

    def test_it_runs_a_query(self) -> None:
        compose = "\n".join(executable_lines(REPO_ROOT / "docker-compose.yml"))
        assert "pg_isready" not in compose, (
            "pg_isready answers for a database that cannot serve; the healthcheck has to "
            "fork a backend and read the catalogue"
        )
        assert "SELECT 1" in compose

    def test_the_user_and_database_are_not_written_twice(self) -> None:
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        assert '-U "$$POSTGRES_USER"' in compose
        assert '-d "$$POSTGRES_DB"' in compose

    def test_it_does_not_depend_on_a_migration_having_run(self) -> None:
        # A table read would make a correctly empty database unhealthy, which would stop
        # the first provision of any new host.
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        for table in ("instruments", "ticks", "ohlcv_bars", "audit_log"):
            assert f"FROM {table}" not in compose


class TestDefinitionsThatExistTwiceAgree:
    """Where a value is written in two files for a legitimate reason, pin them together.

    The rule comes from the coverage defect: two implementations of one rule disagreed
    for a year and every test passed, because each was tested against its own author's
    expectations rather than against the other.
    """

    def test_the_state_directory_default_is_the_same_in_both_scripts(self) -> None:
        pattern = r"TRADINGSYS_ALERT_STATE_DIR:-([^}]+)\}"
        alert = re.search(pattern, ALERT.read_text())
        canary = re.search(pattern, CANARY.read_text())
        assert alert is not None
        assert canary is not None
        assert alert.group(1) == canary.group(1), (
            "alert.sh and canary.sh would keep their state in different places, so the "
            "canary sequence and the firing state would survive different failures"
        )

    def test_the_volume_threshold_default_is_the_same_in_both_checks(self) -> None:
        pattern = r"TRADINGSYS_VOLUME_ALERT_PERCENT:-([0-9]+)\}"
        healthcheck = re.search(pattern, (PROVISION / "healthcheck.sh").read_text())
        assertion = re.search(pattern, (PROVISION / "assert_recording.sh").read_text())
        assert healthcheck is not None
        assert assertion is not None
        assert healthcheck.group(1) == assertion.group(1), (
            "the operator's check and the alert would disagree about where the line is"
        )

    def test_the_state_directory_default_is_what_bootstrap_creates(self) -> None:
        match = re.search(r"TRADINGSYS_ALERT_STATE_DIR:-([^}]+)\}", ALERT.read_text())
        assert match is not None
        assert match.group(1) in BOOTSTRAP.read_text(), (
            "bootstrap creates a state directory the alerter does not use, so the "
            "alerter would silently run without suppression"
        )


class TestTheUnitFailureHandler:
    def test_it_refuses_without_a_unit_name(self) -> None:
        result = run(ALERT_UNIT_FAILED, env=script_env())
        assert result.returncode == 2
        assert "usage" in result.stderr

    def test_an_unreadable_journal_becomes_a_stated_reason(
        self, telegram: StubEndpoint, tmp_path: Path
    ) -> None:
        # journalctl reports an empty log rather than an error when the caller may not
        # read another unit's journal, and an empty body reads exactly like a unit that
        # failed silently. On this development machine there is no such unit at all,
        # which produces the same empty excerpt.
        env = script_env(api=telegram.url, state_dir=tmp_path)
        result = run(ALERT_UNIT_FAILED, "nonexistent-unit.service", env=env)
        assert result.returncode == 0, result.stderr
        text = telegram.received[0].field("text")
        assert "nonexistent-unit.service failed" in text
        assert "no journal output" in text or "nonexistent" in text


@pytest.mark.integration
class TestTheStallQueryRunsAgainstARealDatabase:
    """The query the recording assertion depends on, executed rather than read.

    A shell script holding SQL is the easiest place in this repository for a broken
    query to hide: nothing imports it, the type checker never sees it, and its failure
    mode is an alert that fires every five minutes until the channel is muted, or one
    that never fires at all. So the text is extracted from the script itself and run
    against the real schema. Copying the query into this file would prove only that a
    copy works.
    """

    def _sql(self) -> str:
        body = (PROVISION / "assert_recording.sh").read_text()
        match = re.search(
            r"# --- BEGIN TICK AGE SQL ---\nTICK_AGE_SQL=\"(.+?)\"\n# --- END TICK AGE SQL ---",
            body,
            re.DOTALL,
        )
        assert match is not None, "the markers around the query moved; the test cannot find it"
        return match.group(1)

    async def test_an_empty_table_answers_with_no_rows(self, database: Database) -> None:
        # Which the script reports as "nothing has ever been recorded on this host",
        # a different condition from a stalled feed and worth a different sentence.
        assert await database.fetch(self._sql()) == []

    async def test_it_reports_an_age_in_seconds_per_source(
        self,
        instruments: InstrumentRepository,
        market_data: MarketDataRepository,
        database: Database,
    ) -> None:
        row_id = await instruments.upsert(eurusd())
        now = datetime.now(UTC)
        await market_data.store_ticks(
            row_id,
            TickSource.CTRADER,
            [Quote(eurusd().id, now - timedelta(seconds=30), Decimal("1.085"), Decimal("1.0851"))],
        )
        await market_data.store_ticks(
            row_id,
            TickSource.DUKASCOPY,
            [Quote(eurusd().id, now - timedelta(days=2), Decimal("1.085"), Decimal("1.0851"))],
        )

        rows = await database.fetch(self._sql())
        ages = {row["source"]: row["age_seconds"] for row in rows}
        assert ages[TickSource.CTRADER.value] == pytest.approx(30, abs=5)
        assert ages[TickSource.DUKASCOPY.value] > 86_400

    async def test_the_freshest_source_sorts_first(
        self,
        instruments: InstrumentRepository,
        market_data: MarketDataRepository,
        database: Database,
    ) -> None:
        # The script asserts on the first row and reports the rest. A backfill writing
        # two day old history must never be what the stall threshold is measured
        # against, or a dead live feed would read as healthy.
        row_id = await instruments.upsert(eurusd())
        now = datetime.now(UTC)
        await market_data.store_ticks(
            row_id,
            TickSource.DUKASCOPY,
            [Quote(eurusd().id, now - timedelta(days=2), Decimal("1.085"), Decimal("1.0851"))],
        )
        await market_data.store_ticks(
            row_id,
            TickSource.CTRADER,
            [Quote(eurusd().id, now - timedelta(seconds=5), Decimal("1.085"), Decimal("1.0851"))],
        )
        rows = await database.fetch(self._sql())
        assert rows[0]["source"] == TickSource.CTRADER.value


class TestTheStorageProjection:
    """The arithmetic that reported 12,327 days from five minutes of data.

    A new variety of the class this repository keeps meeting: not a check proving less
    than it claims, but one asserting a number that cannot be true, confidently, beside
    the word pass. Every step of it was individually correct against a denominator that
    had not elapsed.

    The same error was made independently by hand on the same day, dividing a partial
    hour by 3600, which is evidence the shape is easy to fall into rather than a lapse in
    one script. So the fix is structural: the denominator is the observed span, and the
    span is printed beside the result.

    These call the function directly, because no CI database will ever hold an hour of
    ticks and the case that broke cannot otherwise be exercised at all.
    """

    HEALTHCHECK = PROVISION / "healthcheck.sh"
    GIGABYTE = 1_000_000_000

    def project(self, count: int, span_seconds: int, per_row: str = "21.90") -> str:
        volume = 60 * self.GIGABYTE
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{self.HEALTHCHECK}"; project_storage {count} {span_seconds} '
                f'{per_row} {volume} {volume} 70 "steady state"',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_sourcing_the_script_runs_no_checks(self) -> None:
        # The guard that makes the rest of this class possible. Without it, sourcing runs
        # the whole suite against whatever machine the test is on.
        result = subprocess.run(
            ["bash", "-c", f'source "{self.HEALTHCHECK}"; echo sourced'],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "sourced"
        assert "tradingsys host verification" not in result.stdout

    def test_the_rate_comes_from_the_observed_span(self) -> None:
        # The director's own measurement: 53,818 rows between 22:03:05 and 22:19:36.
        # The old arithmetic would have called this 53,818 rows per day, 0.6 per second.
        assert "54.3 rows/s" in self.project(53_818, 991)

    def test_a_four_minute_window_is_not_a_daily_total(self) -> None:
        # The host's own numbers on the day this was found: 19,488 rows in about four
        # minutes, which is 81 per second and not 19,488 per day.
        assert "81.2 rows/s" in self.project(19_488, 240, per_row="243.81")

    def test_the_span_is_reported_beside_the_result(self) -> None:
        # So a reader can see what was divided by, which is the only thing that would
        # have made the original number obviously wrong.
        assert "over 16.5 minutes" in self.project(53_818, 991)

    def test_the_runway_is_days_not_millennia(self) -> None:
        # 54 rows/s compressed on a 60 GB volume is under two years. The reported figure
        # was 12,327 days, which is 33 years, and nothing objected to it.
        projected = self.project(53_818, 991)
        days = int(re.search(r"(\d+) days to full", projected).group(1))  # type: ignore[union-attr]
        assert 300 < days < 1200, projected

    def test_halving_the_rate_doubles_the_runway(self) -> None:
        # The property that makes it a rate at all.
        fast = int(re.search(r"(\d+) days to full", self.project(60_000, 1000)).group(1))  # type: ignore[union-attr]
        slow = int(re.search(r"(\d+) days to full", self.project(30_000, 1000)).group(1))  # type: ignore[union-attr]
        assert slow == pytest.approx(fast * 2, rel=0.02)

    def test_the_same_rate_over_a_longer_window_projects_the_same(self) -> None:
        # A window twice as long with twice the rows is the same rate. The old arithmetic
        # would have doubled the projected consumption.
        assert (
            self.project(60_000, 1000).split(",")[1:] == self.project(120_000, 2000).split(",")[1:]
        )

    def test_a_zero_span_refuses_rather_than_dividing(self) -> None:
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{self.HEALTHCHECK}"; project_storage 1 0 21.90 1000 1000 70 "phase"',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode != 0
        assert "span is zero" in result.stdout

    def test_the_minimum_window_is_an_hour_and_is_configurable(self) -> None:
        body = self.HEALTHCHECK.read_text()
        assert "TRADINGSYS_PROJECTION_MIN_SECONDS:-3600" in body

    def test_headroom_refuses_a_percentage_that_is_not_a_number(self) -> None:
        # df prints nothing for a path that does not exist, and bash reads "" as 0, so
        # this check used to pass on a missing volume while printing " percent full".
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{self.HEALTHCHECK}"; DATA_ROOT=/does/not/exist volume_headroom',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode != 0
        assert "headroom is unknown" in result.stdout
