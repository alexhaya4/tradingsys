#!/usr/bin/env bash
#
# Arm the weekday crypto rate capture to begin at a wall clock instant.
#
# This is a script rather than a transcribed command because the arming has now gone
# wrong twice, in two different ways, and both are the kind of thing a command typed
# from memory reproduces.
#
# The first arming counted a fixed number of seconds from launch, so it began at
# 00:30 rather than 00:00 and put two half length buckets at the ends of a series that
# buckets by absolute UTC hour.
#
# The second used `sleep N` against a wall clock target, which is correct until the
# host suspends. WSL2 froze the VM overnight: the process kept its place in the sleep
# while the wall clock advanced about nine hours, so a capture armed for 23:59:30Z was
# still sleeping at 06:45Z the next morning and would have started at 08:57Z.
#
# So the wait here re-reads the clock instead of trusting an interval. A suspend costs
# at most one poll interval of lateness rather than the whole of its duration.
#
# Usage:
#   scripts/arm_crypto_capture.sh '2026-08-17 07:00:00'   target, UTC
#   scripts/arm_crypto_capture.sh --help

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OUT_DIR=/var/tmp/tradingsys
OUT_FILE="${OUT_DIR}/crypto-rate-weekday.json"
LOG_FILE="${OUT_DIR}/crypto-rate-weekday.log"
HOURS=24.01
POLL_SECONDS=30

usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
}

fail() {
    printf 'arm_crypto_capture: %s\n' "$*" >&2
    exit 1
}

main() {
    if (($# != 1)) || [[ "$1" == "-h" || "$1" == "--help" ]]; then
        usage
        [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && return 0
        fail "exactly one argument is required: the UTC start instant"
    fi

    local target_epoch now
    target_epoch=$(date -u -d "$1 UTC" +%s) || fail "could not parse '$1' as a UTC instant"
    now=$(date -u +%s)
    ((target_epoch > now)) || fail "$1 is not in the future"

    mkdir -p "$OUT_DIR"
    printf 'waiting until %s to start a %s hour capture\n' \
        "$(date -u -d "@${target_epoch}" '+%Y-%m-%dT%H:%M:%SZ')" "$HOURS"

    # Re-read the clock rather than sleeping the whole interval, so a host suspend
    # costs one poll rather than the remainder of the wait.
    while (($(date -u +%s) < target_epoch)); do
        sleep "$POLL_SECONDS"
    done

    printf 'starting capture at %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    exec uv run python scripts/measure_crypto_rate.py --hours "$HOURS" --out "$OUT_FILE"
}

main "$@" >>"$LOG_FILE" 2>&1
