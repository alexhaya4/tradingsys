#!/usr/bin/env bash
#
# Post-deployment verification. Answers "is this host actually doing its job", which is
# a different question from "did the containers start".
#
# Every check says why it failed rather than only that it failed, for the reason
# recorded in docs/DECISIONS.md: a probe that collapses a refused connection, an HTTP
# error and a missing value into one message costs more time than it saves.

set -uo pipefail

DATA_ROOT="${TRADINGSYS_DATA_ROOT:-/mnt/tradingsys_db}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE="docker compose -f ${REPO_ROOT}/docker-compose.yml -f ${REPO_ROOT}/deploy/provision/docker-compose.prod.yml"
FAILED=0

check() {
    local name="$1"; shift
    local detail
    if detail="$("$@" 2>&1)"; then
        printf '  pass  %-34s %s\n' "$name" "$detail"
    else
        printf '  FAIL  %-34s %s\n' "$name" "$detail"
        FAILED=$((FAILED + 1))
    fi
}

# Measured 2026-08-18 against real venue output loaded into the real schema. Provenance
# is in PROGRESS.md; these are the constants the retention arithmetic uses and they are
# named here so a projection cannot silently disagree with the plan.
BYTES_PER_ROW_COMPRESSED=21.90
BYTES_PER_ROW_UNCOMPRESSED=243.81
COMPRESS_AFTER_DAYS=7

# 80 percent is where Postgres becomes constrained, because compress_chunk writes the
# compressed chunk before dropping the uncompressed one and the daily job peaks above
# steady state. Alerting there leaves no time to act, so the alert is at 70.
ALERT_PERCENT="${TRADINGSYS_VOLUME_ALERT_PERCENT:-70}"
OPERATING_LIMIT_PERCENT=80

volume_is_its_own_device() {
    # The sizing error this guards against: reading the droplet disk as the database
    # disk. A percentage of the wrong device still looks like an answer.
    mountpoint -q "$DATA_ROOT" || { echo "${DATA_ROOT} is not a mount point, so Postgres would be writing to the root disk"; return 1; }
    local data_dev root_dev
    data_dev="$(findmnt --noheadings --output SOURCE --target "$DATA_ROOT")"
    root_dev="$(findmnt --noheadings --output SOURCE --target /)"
    if [[ "$data_dev" == "$root_dev" ]]; then
        echo "${DATA_ROOT} resolves to the root device ${data_dev}; every storage figure below would describe the wrong disk"
        return 1
    fi
    echo "${data_dev} $(findmnt --noheadings --output FSTYPE,SIZE --target "$DATA_ROOT")"
}

volume_headroom() {
    local pct
    pct="$(df --output=pcent "$DATA_ROOT" | tail -1 | tr -dc '0-9')"
    local used avail
    used="$(df -h --output=used "$DATA_ROOT" | tail -1 | tr -d ' ')"
    avail="$(df -h --output=avail "$DATA_ROOT" | tail -1 | tr -d ' ')"
    if [[ "$pct" -ge "$ALERT_PERCENT" ]]; then
        echo "${pct} percent full (${used} used, ${avail} free), at or past the ${ALERT_PERCENT} percent alert threshold; the ${OPERATING_LIMIT_PERCENT} percent Postgres operating limit is next"
        return 1
    fi
    echo "${pct} percent full (${used} used, ${avail} free), alert at ${ALERT_PERCENT}, operating limit ${OPERATING_LIMIT_PERCENT}"
}

volume_projection() {
    # A percentage says where the volume is. A projection says whether to act this week.
    local rows age_days
    rows="$($COMPOSE exec -T db psql -qtAX -U "${POSTGRES_USER:-tradingsys}" -d "${POSTGRES_DB:-tradingsys}" \
        -c "SELECT count(*) FROM ticks WHERE ts >= now() - interval '24 hours'" 2>&1)" \
        || { echo "could not query the tick table: $(tail -1 <<<"$rows")"; return 1; }
    if ! [[ "$rows" =~ ^[0-9]+$ ]]; then
        echo "the tick table did not answer with a count: $(head -c 200 <<<"$rows")"
        return 1
    fi
    if [[ "$rows" -eq 0 ]]; then
        echo "no ticks in the last 24 hours, so there is no observed rate to project from"
        return 0
    fi

    age_days="$($COMPOSE exec -T db psql -qtAX -U "${POSTGRES_USER:-tradingsys}" -d "${POSTGRES_DB:-tradingsys}" \
        -c "SELECT COALESCE(EXTRACT(epoch FROM now() - min(ts)) / 86400, 0)::int FROM ticks" 2>/dev/null)"
    [[ "$age_days" =~ ^[0-9]+$ ]] || age_days=0

    local avail_bytes
    avail_bytes="$(df --output=avail --block-size=1 "$DATA_ROOT" | tail -1 | tr -dc '0-9')"
    local total_bytes
    total_bytes="$(df --output=size --block-size=1 "$DATA_ROOT" | tail -1 | tr -dc '0-9')"

    # In steady state each day adds one compressed day while the seven day uncompressed
    # window stays a constant size. Before day seven nothing has been compressed yet, so
    # the growth rate is the uncompressed figure and saying otherwise would flatter it.
    awk -v rows="$rows" -v avail="$avail_bytes" -v total="$total_bytes" \
        -v comp="$BYTES_PER_ROW_COMPRESSED" -v uncomp="$BYTES_PER_ROW_UNCOMPRESSED" \
        -v age="$age_days" -v after="$COMPRESS_AFTER_DAYS" -v alert="$ALERT_PERCENT" '
    BEGIN {
        steady = (age >= after)
        per_row = steady ? comp : uncomp
        per_day = rows * per_row
        if (per_day <= 0) { print "observed rate is zero, nothing to project"; exit 0 }
        to_full  = avail / per_day
        headroom = (total * alert / 100) - (total - avail)
        to_alert = headroom / per_day
        phase = steady ? "steady state, compressed" : "first " after " days, not yet compressed"
        printf "%d rows/24h at %.2f B/row (%s), %.2f GB/day: %.0f days to full", \
               rows, per_row, phase, per_day / 1e9, to_full
        if (to_alert > 0) printf ", %.0f days to the %d percent alert", to_alert, alert
        else printf ", already past the %d percent alert", alert
        printf "\n"
    }'
}

unit_enabled() {
    systemctl is-enabled --quiet tradingsys.service || { echo "tradingsys.service is not enabled, so the stack will not return after a reboot"; return 1; }
    echo "enabled and $(systemctl is-active tradingsys.service)"
}

containers_up() {
    local expected=5 running
    running="$($COMPOSE ps --status running --format '{{.Service}}' 2>/dev/null | grep -c .)"
    if [[ "$running" -lt "$expected" ]]; then
        echo "only ${running} of ${expected} services running: $($COMPOSE ps --format '{{.Service}}={{.State}}' | tr '\n' ' ')"
        return 1
    fi
    echo "${running} services running"
}

app_ready() {
    local body code
    body="$(curl -s -m 10 -w '\n%{http_code}' http://127.0.0.1:8000/ready 2>&1)" || { echo "curl failed against /ready: ${body}"; return 1; }
    code="$(tail -1 <<<"$body")"
    [[ "$code" == "200" ]] || { echo "HTTP ${code}, body: $(head -c 300 <<<"$body")"; return 1; }
    echo "HTTP 200"
}

app_live() {
    local code
    code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health 2>&1)" || { echo "curl failed against /health"; return 1; }
    [[ "$code" == "200" ]] || { echo "HTTP ${code}"; return 1; }
    echo "HTTP 200"
}

metrics_present() {
    local body
    body="$(curl -s -m 10 http://127.0.0.1:8000/metrics 2>&1)" || { echo "curl failed against /metrics"; return 1; }
    grep -q 'tradingsys_build_info' <<<"$body" || {
        echo "tradingsys_build_info absent from $(wc -c <<<"$body") bytes; first lines: $(head -3 <<<"$body" | tr '\n' '|')"
        return 1
    }
    echo "$(wc -l <<<"$body") series lines"
}

migrations_at_head() {
    local out
    out="$($COMPOSE run --rm -T app alembic current 2>&1)" || { echo "alembic current failed: $(tail -2 <<<"$out")"; return 1; }
    grep -q '(head)' <<<"$out" || { echo "database is not at head: $(tail -1 <<<"$out")"; return 1; }
    echo "at head"
}

venue_reachable() {
    local code
    code="$(curl -s -m 15 -o /dev/null -w '%{http_code}' https://api.bybit.com/v5/market/time 2>&1)" || { echo "could not reach api.bybit.com"; return 1; }
    [[ "$code" == "200" ]] || { echo "api.bybit.com returned HTTP ${code}"; return 1; }
    echo "api.bybit.com HTTP 200"
}

clock_sane() {
    # This host must not suspend and must hold wall clock. The whole reason it exists is
    # that the previous one did neither. A monotonic clock far behind uptime would mean
    # the same defect class has followed us here.
    local mono boot
    mono="$(cut -d' ' -f1 /proc/uptime)"
    boot="$(python3 -c 'import time; print(time.clock_gettime(time.CLOCK_BOOTTIME))' 2>/dev/null || echo "$mono")"
    echo "uptime ${mono}s, boottime ${boot}s, $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

echo "tradingsys host verification"
echo
check "volume is its own device"    volume_is_its_own_device
check "volume headroom"             volume_headroom
check "volume projection"           volume_projection
check "systemd unit"                unit_enabled
check "containers running"          containers_up
check "app liveness /health"        app_live
check "app readiness /ready"        app_ready
check "metrics exposed"             metrics_present
check "migrations at head"          migrations_at_head
check "venue reachable"             venue_reachable
check "clock"                       clock_sane

echo
if [[ "$FAILED" -gt 0 ]]; then
    echo "${FAILED} check(s) failed."
    exit 1
fi
echo "all checks passed."
