#!/usr/bin/env bash
#
# Send one canary, then tell the dead man switch it was sent.
#
# WHY THIS EXISTS: the alerting path is the only component that cannot verify itself.
# Every other check on this host asserts something and reports through the alerting
# path; the path has nothing to report through. No test we write can establish that a
# message reaches a phone in Kenya, so its verification has to be the arrival of a
# message rather than any code. That is what this sends.
#
# WHY THE PING MATTERS MORE THAN THE MESSAGE: a canary that arrives proves the path
# works. A canary that does not arrive proves nothing, because silence is
# indistinguishable from a working system with nothing to say. So delivery is reported
# to an external dead man switch, which alerts when the report does not arrive.
#
# The switch is deliberately off this host and off Telegram. It has to survive the
# failures it exists to catch, and those include this host being gone and the bot token
# being wrong, either of which silences anything that runs here or sends there.
#
# One missing ping covers every way the path can break: host down, network down, timer
# disabled, unit never installed, token revoked, chat id wrong, Telegram unreachable.
#
# Environment:
#   TRADINGSYS_ALERT_DEADMAN_URL   ping on successful delivery. Anyone holding this can
#                                  suppress the alert, so it is a secret and lives in
#                                  .env with the others.
#   TRADINGSYS_ALERT_STATE_DIR     where the sequence number lives. Default
#                                  /var/lib/tradingsys, which alert.sh also defaults to.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
ALERT="${HERE}/alert.sh"

STATE_DIR="${TRADINGSYS_ALERT_STATE_DIR:-/var/lib/tradingsys}"
SEQUENCE_FILE="${STATE_DIR}/canary-sequence"
DEADMAN_URL="${TRADINGSYS_ALERT_DEADMAN_URL:-}"

# Nothing below may fail the canary for a reason that is not the alerting path. Where a
# fact cannot be read, the message says so in its place: a canary that says "commit:
# unreadable" still proves delivery, and a canary that dies collecting its own payload
# proves nothing and triggers a false absence alert into the bargain.
unreadable() { echo "unreadable, see the journal on this host"; }

SEQUENCE=0
if [[ -f "$SEQUENCE_FILE" ]]; then
    SEQUENCE="$(head -1 "$SEQUENCE_FILE" 2>/dev/null || echo 0)"
    [[ "$SEQUENCE" =~ ^[0-9]+$ ]] || SEQUENCE=0
fi
SEQUENCE=$((SEQUENCE + 1))
if ! (mkdir -p "$STATE_DIR" 2>/dev/null && echo "$SEQUENCE" >"$SEQUENCE_FILE"); then
    echo "cannot record the canary sequence in ${SEQUENCE_FILE}, so consecutive canaries" >&2
    echo "will carry the same number and a missed one will not be visible in the series." >&2
fi

COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || unreadable)"
BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || unreadable)"
SCHEDULE="$(systemctl show tradingsys-canary.timer -p TimersCalendar --value 2>/dev/null)"
[[ -n "$SCHEDULE" ]] || SCHEDULE="$(unreadable)"
UPTIME="$(uptime -p 2>/dev/null || unreadable)"

BODY="$(printf 'canary %s\n\ndeployed: %s on %s\nhost uptime: %s\nschedule: %s\n\n%s' \
    "$SEQUENCE" "$COMMIT" "$BRANCH" "$UPTIME" "$SCHEDULE" \
    "This message is the only proof that alerts can reach you. If one stops arriving,
the dead man switch reports the absence by email; if that is also silent, assume
nothing about this host and log in.")"

if ! "$ALERT" send "tradingsys canary ${SEQUENCE}" "$BODY"; then
    echo "the canary could not be delivered, so the alerting path is broken right now." >&2
    echo "The dead man switch has deliberately not been pinged: its whole purpose is to" >&2
    echo "report this as an absence." >&2
    exit 1
fi

if [[ -z "$DEADMAN_URL" ]]; then
    echo "the canary was delivered but TRADINGSYS_ALERT_DEADMAN_URL is not set, so" >&2
    echo "nothing off this host is watching for a canary that stops arriving. A missing" >&2
    echo "canary is currently indistinguishable from a quiet system. See the runbook." >&2
    exit 1
fi

PING="$(curl -sS --max-time 20 --retry 3 --retry-delay 5 --retry-connrefused \
    -o /dev/null -w '%{http_code}' "$DEADMAN_URL" 2>&1)"
STATUS="$?"
if [[ "$STATUS" -ne 0 ]]; then
    echo "the canary was delivered but the dead man switch could not be pinged:" >&2
    echo "curl exited ${STATUS}: ${PING}" >&2
    echo "The switch will report an absence that did not happen, which is the safe" >&2
    echo "direction, but the cause is here rather than in the alerting path." >&2
    exit 1
fi
if [[ "$PING" != "200" ]]; then
    echo "the dead man switch refused the ping: HTTP ${PING}" >&2
    echo "Check TRADINGSYS_ALERT_DEADMAN_URL against the check's ping URL." >&2
    exit 1
fi

echo "canary ${SEQUENCE} delivered and dead man switch pinged"
