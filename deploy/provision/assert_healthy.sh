#!/usr/bin/env bash
#
# Is the stack working right now. Run every minute by tradingsys-health.timer.
#
# This exists because tradingsys.service is a oneshot that reports active (exited) once
# the stack has started, which says nothing about the hours afterwards. On 2026-08-19 the
# app crash-looped for four hours behind exactly that. A supervisor on a host chosen for
# running unattended has to be able to say the app is dead without being asked.
#
# It asserts, in order: every expected service exists, none has exhausted its restart cap
# and stopped, and the app reports healthy by its own healthcheck, which probes /ready.
# Exiting non-zero fails the unit, which is what makes the condition visible to
# systemctl, to journald, and to the OnFailure= handler that delivers it.
#
# Raising the alert is deliberately not done here. A unit that fails by dying or by
# timing out cannot report itself, so raising belongs to OnFailure, outside. Clearing is
# done here, because passing is the one event only this script can observe. The unit
# supplies the key as TRADINGSYS_ALERT_KEY, so this script never learns its own name.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_ROOT="${TRADINGSYS_DATA_ROOT:-/mnt/tradingsys_db}"
# Exported for the same reason healthcheck.sh exports it: every compose invocation is a
# child process and the overlay binds db_data through a variable with no default.
export TRADINGSYS_DATA_ROOT="$DATA_ROOT"

COMPOSE=(docker compose
    -f "${REPO_ROOT}/docker-compose.yml"
    -f "${REPO_ROOT}/deploy/provision/docker-compose.prod.yml")

EXPECTED_SERVICES="app db redis prometheus grafana"
FAILURES=()

status_json="$("${COMPOSE[@]}" ps --format json 2>&1)" || {
    echo "cannot read compose state: ${status_json}" >&2
    exit 1
}

for service in $EXPECTED_SERVICES; do
    # One JSON object per line, which is what compose emits for multiple services.
    line="$(grep -F "\"Service\":\"${service}\"" <<<"$status_json" | head -1)"
    if [[ -z "$line" ]]; then
        FAILURES+=("${service}: no container exists")
        continue
    fi
    state="$(sed -n 's/.*"State":"\([^"]*\)".*/\1/p' <<<"$line")"
    health="$(sed -n 's/.*"Health":"\([^"]*\)".*/\1/p' <<<"$line")"
    exit_code="$(sed -n 's/.*"ExitCode":\([0-9-]*\).*/\1/p' <<<"$line")"

    if [[ "$state" != "running" ]]; then
        # A stopped app after a capped restart policy is the deterministic failure the
        # cap exists to surface. Say so rather than only reporting the state.
        FAILURES+=("${service}: state=${state} exit=${exit_code:-unknown}, which for app means it exhausted its restart cap and will not retry")
        continue
    fi
    if [[ -n "$health" && "$health" != "healthy" ]]; then
        FAILURES+=("${service}: running but health=${health}")
    fi
done

if [[ ${#FAILURES[@]} -gt 0 ]]; then
    echo "tradingsys stack is not healthy:" >&2
    printf '  %s\n' "${FAILURES[@]}" >&2
    echo >&2
    echo "last 20 lines from app:" >&2
    "${COMPOSE[@]}" logs app --tail 20 >&2 2>&1 || true
    exit 1
fi

echo "stack healthy: ${EXPECTED_SERVICES// /, }"

# Only if this unit had raised something. alert.sh sends nothing when nothing was
# firing, so the healthy path costs one file check a minute.
if [[ -n "${TRADINGSYS_ALERT_KEY:-}" ]]; then
    "${REPO_ROOT}/deploy/provision/alert.sh" clear "unit:${TRADINGSYS_ALERT_KEY}" \
        "the tradingsys stack is healthy again"
fi
