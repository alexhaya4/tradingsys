#!/usr/bin/env bash
#
# Is the host doing the job it exists for, right now. Run every five minutes by
# tradingsys-recording.timer.
#
# Two conditions, and neither is visible to assert_healthy.sh:
#
#   1. Ticks are still landing in the database. Measured as the age of the newest tick
#      row, never as process liveness. A recorder that is running and recording nothing
#      looks identical from the outside to one that is running and recording everything,
#      and this project has now paid for that confusion five times. The row is the
#      outcome; the process is a step on the way to it.
#
#   2. The volume has room. Storage exhaustion is the one failure that arrives slowly
#      enough to be prevented and stops everything when it lands. healthcheck.sh already
#      computes the same percentage against the same threshold variable, so the two
#      cannot disagree about where the line is.
#
# WHAT THIS DOES NOT CATCH, stated because it becomes wrong later: it asserts on the
# freshest source only, so while crypto is the sole live stream a healthy crypto feed
# would mask a dead forex one. That is the same limitation the gap monitor records for
# provenance, and it has the same remedy: when forex records live, this asserts per
# source. The per source breakdown is already reported below so the day it matters the
# evidence is in the message.
#
# Raising is left to OnFailure=, outside this script, for the reason in assert_healthy.sh.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_ROOT="${TRADINGSYS_DATA_ROOT:-/mnt/tradingsys_db}"
# Exported for the same reason healthcheck.sh exports it: every compose invocation is a
# child process and the overlay binds db_data through a variable with no default.
export TRADINGSYS_DATA_ROOT="$DATA_ROOT"

COMPOSE=(docker compose
    -f "${REPO_ROOT}/docker-compose.yml"
    -f "${REPO_ROOT}/deploy/provision/docker-compose.prod.yml")

# Sized against what it is watching, not chosen round. The supervisor's own quote
# deadline is 30 seconds in config/base.toml, the recorder flushes its batch every 5,
# and a crashed app restarts and reconnects in tens of seconds. Five minutes is ten
# supervisor deadlines plus a full restart cycle, so anything this catches has already
# survived the recovery the system does for itself.
STALL_SECONDS="${TRADINGSYS_ALERT_TICK_STALL_SECONDS:-300}"
# The same variable healthcheck.sh reads, so one threshold governs both.
ALERT_PERCENT="${TRADINGSYS_VOLUME_ALERT_PERCENT:-70}"

# Extracted verbatim by tests/test_alerting_path.py and executed against a real database
# by the integration suite, so a query that cannot run cannot ship. A broken query here
# fails open in the worst way: it reports a problem that is not there every five minutes
# until the channel is muted.
# --- BEGIN TICK AGE SQL ---
TICK_AGE_SQL="SELECT source, EXTRACT(epoch FROM now() - max(ts))::bigint AS age_seconds FROM ticks GROUP BY source ORDER BY age_seconds ASC"
# --- END TICK AGE SQL ---

FAILURES=()
DETAIL=()

psql_query() {
    "${COMPOSE[@]}" exec -T db psql -qtAX \
        -U "${POSTGRES_USER:-tradingsys}" -d "${POSTGRES_DB:-tradingsys}" -c "$1" 2>&1
}

check_ticks() {
    local rows
    if ! rows="$(psql_query "$TICK_AGE_SQL")"; then
        FAILURES+=("cannot read the tick table, so whether anything is being recorded is unknown: $(tail -2 <<<"$rows" | tr '\n' ' ')")
        return
    fi
    if [[ -z "${rows//[[:space:]]/}" ]]; then
        FAILURES+=("the tick table is empty: nothing has ever been recorded on this host")
        return
    fi

    local freshest="" line source age
    while IFS='|' read -r source age; do
        [[ -n "$source" ]] || continue
        if ! [[ "$age" =~ ^-?[0-9]+$ ]]; then
            FAILURES+=("the tick table answered with something that is not an age: $(head -c 200 <<<"$rows")")
            return
        fi
        DETAIL+=("${source} newest ${age}s ago")
        [[ -z "$freshest" ]] && freshest="$age"
    done <<<"$rows"

    if [[ -z "$freshest" ]]; then
        FAILURES+=("the tick table answered with no usable rows: $(head -c 200 <<<"$rows")")
        return
    fi
    if [[ "$freshest" -gt "$STALL_SECONDS" ]]; then
        FAILURES+=("no tick has been recorded for ${freshest}s, past the ${STALL_SECONDS}s stall threshold. The stack can be healthy and recording nothing, which is what this checks and assert_healthy.sh cannot")
    fi
}

check_volume() {
    local pct used avail
    if ! pct="$(df --output=pcent "$DATA_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')" || [[ -z "$pct" ]]; then
        FAILURES+=("cannot read the volume at ${DATA_ROOT}, so headroom is unknown")
        return
    fi
    used="$(df -h --output=used "$DATA_ROOT" | tail -1 | tr -d ' ')"
    avail="$(df -h --output=avail "$DATA_ROOT" | tail -1 | tr -d ' ')"
    DETAIL+=("volume ${pct} percent full, ${avail} free")
    if [[ "$pct" -ge "$ALERT_PERCENT" ]]; then
        FAILURES+=("the data volume is ${pct} percent full (${used} used, ${avail} free), at or past the ${ALERT_PERCENT} percent threshold. Postgres becomes constrained at 80 because compression writes the new chunk before dropping the old one. Run deploy/provision/healthcheck.sh for the projection in days")
    fi
}

check_ticks
check_volume

if [[ ${#FAILURES[@]} -gt 0 ]]; then
    echo "tradingsys is not recording as expected:" >&2
    printf '  %s\n' "${FAILURES[@]}" >&2
    if [[ ${#DETAIL[@]} -gt 0 ]]; then
        echo >&2
        printf '  %s\n' "${DETAIL[@]}" >&2
    fi
    exit 1
fi

printf 'recording: %s\n' "$(IFS=', '; echo "${DETAIL[*]}")"

if [[ -n "${TRADINGSYS_ALERT_KEY:-}" ]]; then
    "${REPO_ROOT}/deploy/provision/alert.sh" clear "unit:${TRADINGSYS_ALERT_KEY}" \
        "tradingsys is recording again"
fi
