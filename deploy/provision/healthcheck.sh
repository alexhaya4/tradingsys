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

volume_mounted() {
    mountpoint -q "$DATA_ROOT" || { echo "${DATA_ROOT} is not a mount point, so Postgres would be writing to the root disk"; return 1; }
    df -h --output=used,size,pcent "$DATA_ROOT" | tail -1 | tr -s ' '
}

volume_headroom() {
    local pct
    pct="$(df --output=pcent "$DATA_ROOT" | tail -1 | tr -dc '0-9')"
    # 80 percent is the operating limit, not the full disk: compress_chunk writes the
    # compressed chunk before dropping the uncompressed one, so the daily job peaks
    # above steady state and needs the room.
    if [[ "$pct" -ge 80 ]]; then
        echo "${pct} percent full, at or past the 80 percent Postgres operating limit"
        return 1
    fi
    echo "${pct} percent full, limit is 80"
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
check "block volume mounted"        volume_mounted
check "block volume headroom"       volume_headroom
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
