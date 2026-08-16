#!/usr/bin/env bash
#
# The single executable verification path for this repository.
#
# Everything that proves the system works runs from here: the stack, the migrations,
# the linters, the type checker, and both test suites. The README points at this script
# rather than transcribing its steps, and CI invokes this same script rather than
# reimplementing it, so there is one path and it cannot drift from itself.
#
# If you find yourself running a verification command that is not in this script, that
# command belongs in this script.
#
# Usage:
#   scripts/verify.sh              run everything, leave the stack running
#   scripts/verify.sh --fresh      destroy the compose volumes first, so the run
#                                  provisions a database from empty. Destructive.
#   scripts/verify.sh --down       run everything, then tear the stack down
#   scripts/verify.sh --dry-run    list the steps without executing them
#   scripts/verify.sh --help       this message

# -E propagates the ERR trap into functions. Without it the trap is silently skipped
# for every failure that happens inside a step, which is all of them, and the run ends
# with no indication of which step failed.
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DRY_RUN=false
TEAR_DOWN=false
FRESH=false
CURRENT_STEP="argument parsing"

# Secrets the stack and the host side tooling both need. Sourced from .env locally and
# injected directly in CI, which is why the script requires the variables rather than
# the file.
REQUIRED_VARS=(POSTGRES_PASSWORD TRADINGSYS_DATABASE__PASSWORD GRAFANA_PASSWORD)

DB_READY_TIMEOUT_SECONDS=60
APP_READY_TIMEOUT_SECONDS=90
PROMETHEUS_TIMEOUT_SECONDS=90
HTTP_TIMEOUT_SECONDS=10

usage() {
    # The header comment block is the help text, so the two cannot disagree. Printing
    # stops at the first line that is not a comment.
    awk '
        NR == 1 { next }
        /^#/ { sub(/^# ?/, ""); print; next }
        { exit }
    ' "${BASH_SOURCE[0]}"
}

log() { printf '%s\n' "$*"; }
fail() { printf 'verify: %s\n' "$*" >&2; exit 1; }

on_error() {
    printf '\nverify: FAILED during step: %s\n' "$CURRENT_STEP" >&2
}
trap on_error ERR

step() {
    local name="$1"
    shift
    CURRENT_STEP="$name"
    printf '\n=== %s ===\n' "$name"
    if [[ "$DRY_RUN" == true ]]; then
        printf '(dry run, not executed)\n'
        return 0
    fi
    "$@"
}

HTTP_BODY=""

http_get() {
    # Fetch a URL into HTTP_BODY, aborting with curl's own exit status if the
    # transport failed, so that a refused connection is never reported as missing
    # content.
    #
    # Deliberately not `curl URL | grep -q PATTERN`. That pipeline has two defects and
    # both cost real time.
    #
    # It destroys evidence. A refused connection, an HTTP error, a broken pipe, and a
    # body that genuinely lacks the pattern all produce one identical message and no
    # trace of what came back. CI run 31939779488 failed that way and had to be
    # re-run to classify, because the log could not distinguish the four.
    #
    # And it can fail while the pattern is present. `grep -q` exits at its first
    # match; if curl is still writing it takes EPIPE, exits 23, and under `pipefail`
    # that fails the pipeline. The series this script looks for is the first one in
    # the payload, which makes that window as wide as it can be.
    #
    # stderr is folded into the capture rather than sent to a temporary file: `curl
    # -fsS` is silent on success, so on failure HTTP_BODY carries curl's own
    # diagnosis and there is nothing to clean up.
    local url="$1" status=0
    HTTP_BODY=$(curl -fsS --max-time "$HTTP_TIMEOUT_SECONDS" "$url" 2>&1) || status=$?
    if ((status != 0)); then
        fail "could not fetch ${url}: curl exited ${status}: ${HTTP_BODY}"
    fi
}

# ---------------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------------

preflight() {
    local missing=()
    for tool in docker uv curl; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    if ((${#missing[@]})); then
        fail "these tools are required and were not found: ${missing[*]}"
    fi
    docker compose version >/dev/null 2>&1 ||
        fail "docker compose v2 is required; 'docker compose version' failed"
    log "docker, uv, and curl are present"
}

load_secrets() {
    # .env is read by docker compose automatically but never by the application, which
    # accepts secrets only from the environment or the secrets directory. Sourcing it
    # here is what gives the host side tooling the same values the containers get.
    if [[ -f .env ]]; then
        log "sourcing .env for host side tooling"
        set -a
        # shellcheck disable=SC1091
        . ./.env
        set +a
    else
        log "no .env file; expecting the required variables to be set already"
    fi

    local missing=()
    for name in "${REQUIRED_VARS[@]}"; do
        [[ -n "${!name:-}" ]] || missing+=("$name")
    done
    if ((${#missing[@]})); then
        fail "$(
            cat <<MESSAGE
these variables are not set: ${missing[*]}

Locally, copy .env.example to .env and fill it in; this script sources it.
In CI, inject them as environment variables.

The application never reads secrets from a configuration file, so there is no
file to put them in other than .env, which only docker compose and this script
consume.
MESSAGE
        )"
    fi
    log "required variables are present"
}

destroy_volumes() {
    # The first provision is the path that broke once and the path least often
    # exercised, because every developer machine has a database that already exists in
    # some shape. A migration bug that only appears against an empty volume would
    # otherwise be met for the first time in production.
    #
    # Destructive by design, which is why it is opt in locally. CI always passes it,
    # since CI has nothing to lose and everything to prove.
    log "destroying compose volumes: this run provisions from empty"
    docker compose down -v
}

start_datastores() {
    docker compose up -d db redis
    local waited=0
    until postgres_answers_over_tcp; do
        waited=$((waited + 1))
        if ((waited > DB_READY_TIMEOUT_SECONDS)); then
            docker compose logs --tail 50 db >&2
            fail "postgres did not become ready within ${DB_READY_TIMEOUT_SECONDS}s"
        fi
        sleep 1
    done
    log "postgres is answering queries over TCP after ${waited}s"
}

postgres_answers_over_tcp() {
    # Deliberately not `docker compose exec pg_isready`. On a first provision the
    # postgres image runs a temporary server for initdb that listens on the container's
    # unix socket but not on TCP, so pg_isready reports ready, the real server then
    # restarts, and the next connection fails. That race is invisible on any machine
    # whose volume already exists, which is most of them.
    #
    # Connecting over TCP the same way the application does cannot be fooled by it.
    uv run python - <<'PROBE' >/dev/null 2>&1
import asyncio
import os
import sys

import asyncpg


async def main() -> None:
    connection = await asyncpg.connect(
        host="localhost",
        port=int(os.environ.get("TRADINGSYS_DATABASE__PORT", "5432")),
        user="tradingsys",
        password=os.environ["TRADINGSYS_DATABASE__PASSWORD"],
        database="tradingsys",
        timeout=3,
    )
    try:
        if await connection.fetchval("SELECT 1") != 1:
            sys.exit(1)
    finally:
        await connection.close()


asyncio.run(main())
PROBE
}

create_test_database() {
    local exists
    exists=$(docker compose exec -T db psql -U tradingsys -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname = 'tradingsys_test'")
    if [[ "$exists" == "1" ]]; then
        log "tradingsys_test already exists"
        return 0
    fi
    docker compose exec -T db psql -U tradingsys -d postgres \
        -c 'CREATE DATABASE tradingsys_test OWNER tradingsys'
    log "created tradingsys_test"
}

migrate() {
    # Both databases are migrated, and the environment is named explicitly each time.
    # The app container refuses to start against an unmigrated development database,
    # and the integration suite needs the test database at head.
    #
    # TRADINGSYS_DATABASE__HOST is overridden for development because
    # config/development.toml points at the compose service name, which only resolves
    # inside the compose network. config/test.toml already uses localhost.
    log "migrating development"
    TRADINGSYS_APP__ENVIRONMENT=development \
        TRADINGSYS_DATABASE__HOST=localhost \
        uv run alembic upgrade head
    log "migrating test"
    TRADINGSYS_APP__ENVIRONMENT=test uv run alembic upgrade head
}

check_formatting() {
    uv run ruff format --check .
}

check_lint() {
    uv run ruff check .
}

check_types() {
    uv run mypy
}

run_unit_tests() {
    uv run pytest -q
}

run_integration_tests() {
    TRADINGSYS_APP__ENVIRONMENT=test uv run pytest -m integration -q
}

start_full_stack() {
    docker compose up -d --build
    local waited=0 status=0 reason
    until curl -fsS --max-time "$HTTP_TIMEOUT_SECONDS" \
        http://localhost:8000/ready >/dev/null 2>&1; do
        waited=$((waited + 1))
        if ((waited > APP_READY_TIMEOUT_SECONDS)); then
            docker compose logs --tail 50 app >&2
            # One further attempt with the error kept, so the failure says why the app
            # never answered rather than only that it never did.
            reason=$(curl -fsS --max-time "$HTTP_TIMEOUT_SECONDS" \
                http://localhost:8000/ready 2>&1) || status=$?
            fail "the app did not report ready within ${APP_READY_TIMEOUT_SECONDS}s; \
last attempt, curl exit ${status}: ${reason}"
        fi
        sleep 1
    done
    log "the app reported ready after ${waited}s"
}

probe_endpoints() {
    local path
    for path in /health /ready; do
        http_get "http://localhost:8000${path}"
        printf '%s -> %s\n' "$path" "$HTTP_BODY"
        grep -q '"status":"pass"' <<<"$HTTP_BODY" ||
            fail "${path} answered but did not report pass: ${HTTP_BODY}"
    done

    http_get http://localhost:8000/metrics
    grep -q '^tradingsys_build_info' <<<"$HTTP_BODY" || fail "$(
        cat <<MESSAGE
/metrics answered ${#HTTP_BODY} bytes but exposed no tradingsys_build_info sample line.

Retrying is not the answer and a more patient probe would be the wrong fix. Readiness
passed immediately before this, and /ready and /metrics are two routes on one app
closing over one metric registry that is built before the port is bound, so if the
port answers at all the series is already registered and set. There is no window in
which this can be absent for timing reasons, which means something else is wrong and
a retry would only hide it.

The first twenty lines of what came back:
$(head -20 <<<"$HTTP_BODY")
MESSAGE
    )"
    log "/metrics exposes the expected series"
}

check_prometheus() {
    # This one polls rather than asserting, because prometheus is expected to be
    # unreachable for the first few seconds, so http_get's abort-on-failure is wrong
    # here and curl's status is kept instead.
    local waited=0 targets status
    while true; do
        status=0
        targets=$(curl -fsS --max-time "$HTTP_TIMEOUT_SECONDS" \
            'http://localhost:9090/api/v1/targets?state=active' 2>&1) || status=$?

        # Every condition sits inside the `if`, where a failing command is exempt from
        # `set -e`, so a target that is reported neither up nor down means retry.
        if ((status == 0)) &&
            ! grep -q '"health":"down"' <<<"$targets" &&
            grep -q '"health":"up"' <<<"$targets"; then
            break
        fi

        waited=$((waited + 1))
        if ((waited > PROMETHEUS_TIMEOUT_SECONDS)); then
            # The previous version sent curl's error to /dev/null, so a prometheus
            # that never answered at all timed out with an empty document and no
            # stated reason.
            printf 'last attempt, curl exit %s:\n%s\n' "$status" "$targets" >&2
            fail "prometheus targets did not all come up within ${PROMETHEUS_TIMEOUT_SECONDS}s"
        fi
        sleep 1
    done
    log "every prometheus target is up after ${waited}s"
}

tear_down() {
    docker compose down
}

# ---------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------

main() {
    while (($#)); do
        case "$1" in
            --dry-run) DRY_RUN=true ;;
            --down) TEAR_DOWN=true ;;
            --fresh) FRESH=true ;;
            -h | --help)
                usage
                return 0
                ;;
            *) fail "unknown argument: $1 (try --help)" ;;
        esac
        shift
    done

    if [[ "$DRY_RUN" == true ]]; then
        log "verify: dry run, listing steps only"
    fi
    if [[ "$FRESH" == true && "$DRY_RUN" == false ]]; then
        log "verify: --fresh will destroy the compose volumes, including any local data"
    fi

    step "preflight" preflight
    step "load secrets" load_secrets
    if [[ "$FRESH" == true ]]; then
        step "destroy volumes" destroy_volumes
    fi
    step "start datastores" start_datastores
    step "create test database" create_test_database
    step "apply migrations" migrate
    step "check formatting" check_formatting
    step "check lint" check_lint
    step "check types" check_types
    step "run unit tests" run_unit_tests
    step "run integration tests" run_integration_tests
    step "start full stack" start_full_stack
    step "probe endpoints" probe_endpoints
    step "check prometheus targets" check_prometheus

    if [[ "$TEAR_DOWN" == true ]]; then
        step "tear down stack" tear_down
    fi

    CURRENT_STEP="reporting"
    if [[ "$DRY_RUN" == true ]]; then
        printf '\nverify: dry run complete, nothing was executed\n'
    else
        printf '\nverify: every step passed\n'
        if [[ "$TEAR_DOWN" == false ]]; then
            printf 'the stack is still running; stop it with: docker compose down\n'
        fi
    fi
}

main "$@"
