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
# Exported, not merely assigned. Every compose command below is a child process, and the
# production overlay binds db_data through ${TRADINGSYS_DATA_ROOT:?...} with no default,
# so an unexported value fails interpolation in the child and every compose based check
# fails for a reason that has nothing to do with what it was checking.
export TRADINGSYS_DATA_ROOT="$DATA_ROOT"
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
    pct="$(df --output=pcent "$DATA_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')"
    # An empty percentage is what df returns for a path that does not exist, and bash
    # reads "" as 0 in an arithmetic comparison, so this check used to pass on a missing
    # volume while printing " percent full ( used,  free)". Same family as the projection
    # that reported 12,327 days: not proving less than it claims, but stating a number
    # that cannot be true and being believed.
    if ! [[ "$pct" =~ ^[0-9]+$ ]]; then
        echo "df reported no usage figure for ${DATA_ROOT}, so headroom is unknown. The path is most likely absent or not mounted"
        return 1
    fi
    local used avail
    used="$(df -h --output=used "$DATA_ROOT" | tail -1 | tr -d ' ')"
    avail="$(df -h --output=avail "$DATA_ROOT" | tail -1 | tr -d ' ')"
    if [[ "$pct" -ge "$ALERT_PERCENT" ]]; then
        echo "${pct} percent full (${used} used, ${avail} free), at or past the ${ALERT_PERCENT} percent alert threshold; the ${OPERATING_LIMIT_PERCENT} percent Postgres operating limit is next"
        return 1
    fi
    echo "${pct} percent full (${used} used, ${avail} free), alert at ${ALERT_PERCENT}, operating limit ${OPERATING_LIMIT_PERCENT}"
}

# The observed window has to be long enough to be a rate rather than a moment. One hour
# is the minimum because anything shorter is a sample of one market condition, and this
# number is used to decide whether to buy storage this week.
PROJECTION_MIN_SECONDS="${TRADINGSYS_PROJECTION_MIN_SECONDS:-3600}"

project_storage() {
    # Pure arithmetic, separated from the query so it can be exercised with fabricated
    # inputs. THE DEFECT THIS SHAPE EXISTS TO PREVENT: the previous version counted rows
    # in the last 24 hours and used that count as a daily rate. On a database holding
    # five minutes of data it reported a rate 288 times too small and a runway of 12,327
    # days, and nothing objected, because every step of the arithmetic was individually
    # correct against a denominator that had not elapsed.
    #
    # The same error was made independently by hand on the same day, dividing a partial
    # hour by 3600, so the remedy is structural rather than a matter of care: the
    # denominator is always the observed span, and the span is always reported beside the
    # result so a reader can see what it was divided by.
    local count="$1" span="$2" per_row="$3" avail="$4" total="$5" alert="$6" phase="$7"
    awk -v count="$count" -v span="$span" -v per_row="$per_row" -v avail="$avail" \
        -v total="$total" -v alert="$alert" -v phase="$phase" '
    BEGIN {
        if (span <= 0) { print "observed span is zero, nothing to project"; exit 1 }
        per_second = count / span
        per_day    = per_second * 86400 * per_row
        if (per_day <= 0) { print "observed rate is zero, nothing to project"; exit 1 }
        to_full  = avail / per_day
        headroom = (total * alert / 100) - (total - avail)
        to_alert = headroom / per_day
        printf "%d rows over %.1f minutes, %.1f rows/s, %.2f B/row (%s), %.2f GB/day: %.0f days to full", \
               count, span / 60, per_second, per_row, phase, per_day / 1e9, to_full
        if (to_alert > 0) printf ", %.0f days to the %d percent alert", to_alert, alert
        else printf ", already past the %d percent alert", alert
        printf "\n"
    }'
}

volume_projection() {
    # A percentage says where the volume is. A projection says whether to act this week,
    # and only if it divides by time that actually elapsed.
    local answer count span age_days
    answer="$($COMPOSE exec -T db psql -qtAX -U "${POSTGRES_USER:-tradingsys}" -d "${POSTGRES_DB:-tradingsys}" \
        -c "SELECT count(*), COALESCE(EXTRACT(epoch FROM max(ts) - min(ts))::bigint, 0) FROM ticks WHERE ts >= now() - interval '24 hours'" 2>&1)" \
        || { echo "could not query the tick table: $(tail -1 <<<"$answer")"; return 1; }
    IFS='|' read -r count span <<<"$answer"
    if ! [[ "$count" =~ ^[0-9]+$ && "$span" =~ ^[0-9]+$ ]]; then
        echo "the tick table did not answer with a count and a span: $(head -c 200 <<<"$answer")"
        return 1
    fi
    if [[ "$count" -eq 0 ]]; then
        echo "no ticks in the last 24 hours, so there is no observed rate to project from"
        return 0
    fi
    if [[ "$span" -lt "$PROJECTION_MIN_SECONDS" ]]; then
        # Reported rather than computed. A projection from a few minutes is not a
        # conservative estimate, it is a number with no relationship to the answer, and
        # printing one beside the word pass is how 12,327 days went unchallenged.
        echo "insufficient observation window: ${count} rows spanning $((span / 60)) minutes, against a $((PROJECTION_MIN_SECONDS / 60)) minute minimum. No projection made"
        return 0
    fi

    age_days="$($COMPOSE exec -T db psql -qtAX -U "${POSTGRES_USER:-tradingsys}" -d "${POSTGRES_DB:-tradingsys}" \
        -c "SELECT COALESCE(EXTRACT(epoch FROM now() - min(ts)) / 86400, 0)::int FROM ticks" 2>/dev/null)"
    [[ "$age_days" =~ ^[0-9]+$ ]] || age_days=0

    local avail_bytes total_bytes per_row phase
    avail_bytes="$(df --output=avail --block-size=1 "$DATA_ROOT" | tail -1 | tr -dc '0-9')"
    total_bytes="$(df --output=size --block-size=1 "$DATA_ROOT" | tail -1 | tr -dc '0-9')"

    # In steady state each day adds one compressed day while the seven day uncompressed
    # window stays a constant size. Before day seven nothing has been compressed yet, so
    # the growth rate is the uncompressed figure and saying otherwise would flatter it.
    if [[ "$age_days" -ge "$COMPRESS_AFTER_DAYS" ]]; then
        per_row="$BYTES_PER_ROW_COMPRESSED"
        phase="steady state, compressed"
    else
        per_row="$BYTES_PER_ROW_UNCOMPRESSED"
        phase="first ${COMPRESS_AFTER_DAYS} days, not yet compressed"
    fi
    project_storage "$count" "$span" "$per_row" "$avail_bytes" "$total_bytes" "$ALERT_PERCENT" "$phase"
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

# name -> function, in the order they run. One list, so --only and the full run cannot
# disagree about what a check is called or what it does.
CHECKS=(
    "volume is its own device:volume_is_its_own_device"
    "volume headroom:volume_headroom"
    "volume projection:volume_projection"
    "systemd unit:unit_enabled"
    "containers running:containers_up"
    "app liveness /health:app_live"
    "app readiness /ready:app_ready"
    "metrics exposed:metrics_present"
    "migrations at head:migrations_at_head"
    "venue reachable:venue_reachable"
    "clock:clock_sane"
)

main() {
    local only=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            # Runs one check by name. The verification path uses it to exercise the
            # checks that need no systemd and no mounted volume, which is how a check
            # gets watched failing somewhere other than a production host.
            --only) only="${2:?--only needs a check name}"; shift 2 ;;
            --list) printf '%s\n' "${CHECKS[@]%%:*}"; return 0 ;;
            -h|--help) echo "usage: healthcheck.sh [--only NAME] [--list]"; return 0 ;;
            *) echo "unknown argument: $1" >&2; return 2 ;;
        esac
    done

    echo "tradingsys host verification"
    echo

    local entry name function ran=0
    for entry in "${CHECKS[@]}"; do
        name="${entry%%:*}"
        function="${entry##*:}"
        if [[ -n "$only" && "$name" != "$only" ]]; then
            continue
        fi
        check "$name" "$function"
        ran=$((ran + 1))
    done

    if [[ -n "$only" && "$ran" -eq 0 ]]; then
        echo "no check is named '${only}'. Names:" >&2
        printf '  %s\n' "${CHECKS[@]%%:*}" >&2
        return 2
    fi

    echo
    if [[ "$FAILED" -gt 0 ]]; then
        echo "${FAILED} check(s) failed."
        return 1
    fi
    echo "all checks passed."
}

# Sourcing this file defines the checks without running them, so the arithmetic in
# project_storage can be exercised with fabricated inputs rather than only against a
# database that happens to hold the right amount of history. That is the only way the
# case this file got wrong, a window shorter than the denominator, can be tested at all:
# no CI database will ever hold an hour of ticks.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
    exit $?
fi
