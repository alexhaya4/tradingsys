#!/usr/bin/env bash
#
# Alert state: whether a condition is already firing, when it may say so again, and
# whether it has recovered. notify.sh sends one message and knows none of that.
#
# WHAT BREAKS IF THIS IS REMOVED: the health assertion runs every minute, so a stack
# that stays down for four hours delivers 240 identical messages. A channel that does
# that gets muted, and a muted channel is the same as no channel, which is the failure
# the whole path exists to prevent. The same argument makes the recovery message
# mandatory rather than a nicety: an operator who is told a thing broke and never told
# it healed has to go and look, and a channel you have to verify by hand is not one you
# trust at three in the morning.
#
# Usage:
#   alert.sh raise KEY SUBJECT [BODY]   sends the first time, then at most once per
#                                       cooldown while the condition persists
#   alert.sh clear KEY [SUBJECT]        sends a recovery only if KEY was firing
#   alert.sh send SUBJECT [BODY]        unconditional, for a message that is not a
#                                       condition, such as the canary
#
# Raise and clear are called from different places on purpose. A failure is raised by
# systemd's OnFailure= handler, from outside the failing unit, because a unit that fails
# by dying cannot report its own death. A recovery is cleared by the assertion itself,
# from inside, because passing is a thing only the assertion can observe. Each is placed
# where the event it reports is visible.
#
# Environment:
#   TRADINGSYS_ALERT_REPEAT_MINUTES   how long before a persisting condition repeats.
#                                     Default 30, which is 8 messages across a four hour
#                                     outage: enough to convey that it is still down,
#                                     few enough to stay readable.
#   TRADINGSYS_ALERT_STATE_DIR        where firing state lives. Default
#                                     /var/lib/tradingsys, created by bootstrap.sh.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="${HERE}/notify.sh"

REPEAT_MINUTES="${TRADINGSYS_ALERT_REPEAT_MINUTES:-30}"
STATE_DIR="${TRADINGSYS_ALERT_STATE_DIR:-/var/lib/tradingsys}/alerts"

usage() {
    echo "usage: alert.sh raise KEY SUBJECT [BODY]" >&2
    echo "       alert.sh clear KEY [SUBJECT]" >&2
    echo "       alert.sh send SUBJECT [BODY]" >&2
    exit 2
}

# A key becomes a file name, so it may not carry a path separator. Rewritten rather than
# rejected: the caller is a systemd unit name and rejecting it would lose the alert.
state_file() {
    printf '%s/%s' "$STATE_DIR" "$(tr -c 'A-Za-z0-9._-' '_' <<<"$1")"
}

read_field() {
    # Explicit parsing rather than sourcing. This file is written by this script, but a
    # file that is executed is a file that can be made to execute something else.
    sed -n "s/^${2}=//p" "$1" | head -1
}

ensure_state_dir() {
    mkdir -p "$STATE_DIR" 2>/dev/null && [[ -w "$STATE_DIR" ]] && return 0
    echo "alert state directory ${STATE_DIR} is missing or not writable, so this alert" >&2
    echo "cannot be suppressed, repeated, or cleared. Every occurrence will be sent." >&2
    echo "Fix with: sudo install -d -o \$(id -un) -g \$(id -gn) ${STATE_DIR%/alerts}" >&2
    return 1
}

now_epoch() { date -u +%s; }

human_duration() {
    local seconds="$1"
    printf '%dh%02dm' $((seconds / 3600)) $(((seconds % 3600) / 60))
}

do_raise() {
    local key="$1" subject="$2" body="${3:-}"
    local file has_state=1
    ensure_state_dir || has_state=0
    file="$(state_file "$key")"

    local now first last_sent count
    now="$(now_epoch)"
    first="$now"; last_sent=0; count=0
    if [[ "$has_state" -eq 1 && -f "$file" ]]; then
        first="$(read_field "$file" first)"
        last_sent="$(read_field "$file" last_sent)"
        count="$(read_field "$file" count)"
        # A truncated or hand-edited state file must not stop an alert. Treat anything
        # unparseable as "never sent", which errs towards delivering.
        [[ "$first" =~ ^[0-9]+$ ]] || first="$now"
        [[ "$last_sent" =~ ^[0-9]+$ ]] || last_sent=0
        [[ "$count" =~ ^[0-9]+$ ]] || count=0
    fi

    local cooldown=$((REPEAT_MINUTES * 60))
    local due=$((last_sent + cooldown))
    if [[ "$last_sent" -gt 0 && "$now" -lt "$due" ]]; then
        # Suppressed, not lost. The condition is still recorded in the journal by the
        # assertion that failed; what is suppressed is the delivery, not the evidence.
        echo "alert suppressed for ${key}: sent $(human_duration $((now - last_sent))) ago," \
             "next repeat due $(date -u -d "@${due}" +%Y-%m-%dT%H:%M:%SZ)"
        return 0
    fi

    local prefix=""
    if [[ "$count" -gt 0 ]]; then
        prefix="STILL FAILING after $(human_duration $((now - first))), alert $((count + 1)): "
    fi

    if ! "$NOTIFY" "${prefix}${subject}" "$body"; then
        # Record the condition as firing with last_sent unset, so the next occurrence
        # retries immediately rather than waiting out a cooldown on a message that was
        # never delivered.
        if [[ "$has_state" -eq 1 ]]; then
            printf 'first=%s\nlast_sent=0\ncount=%s\n' "$first" "$count" >"$file"
        fi
        return 1
    fi

    if [[ "$has_state" -eq 1 ]]; then
        printf 'first=%s\nlast_sent=%s\ncount=%s\n' "$first" "$now" "$((count + 1))" >"$file"
    else
        return 1
    fi
}

do_clear() {
    local key="$1" subject="${2:-}"
    local file
    ensure_state_dir || return 1
    file="$(state_file "$key")"
    [[ -f "$file" ]] || return 0

    local now first count
    now="$(now_epoch)"
    first="$(read_field "$file" first)"
    count="$(read_field "$file" count)"
    [[ "$first" =~ ^[0-9]+$ ]] || first="$now"
    [[ "$count" =~ ^[0-9]+$ ]] || count=0

    local text="${subject:-${key} has recovered}"
    if ! "$NOTIFY" "RECOVERED: ${text}" \
        "was failing for $(human_duration $((now - first))), ${count} alert(s) sent"; then
        # State is kept deliberately. A recovery that was not delivered has not
        # happened as far as anyone reading the channel is concerned, so the next pass
        # tries again rather than leaving a silent transition.
        return 1
    fi
    rm -f "$file"
}

COMMAND="${1:-}"
case "$COMMAND" in
    raise)
        [[ $# -ge 3 ]] || usage
        do_raise "$2" "$3" "${4:-}"
        ;;
    clear)
        [[ $# -ge 2 ]] || usage
        do_clear "$2" "${3:-}"
        ;;
    send)
        [[ $# -ge 2 ]] || usage
        "$NOTIFY" "$2" "${3:-}"
        ;;
    *)
        usage
        ;;
esac
