#!/usr/bin/env bash
#
# Turn a failed systemd unit into a delivered alert. Started by
# tradingsys-alert@.service, which every supervised unit names in its OnFailure=.
#
# WHY THIS RUNS OUTSIDE THE FAILING UNIT: a unit that fails by dying, by timing out, or
# by having its script deleted cannot report any of those things about itself. OnFailure
# fires for all of them, including the ones where nothing of ours ran at all, which is
# exactly the set an in-process alerter misses.
#
# WHAT BREAKS IF THE JOURNAL IS UNREADABLE: the alert still goes out, carrying a note
# saying why it has no detail. journalctl shows an empty log rather than an error when
# the caller may not read other units' journals, so an unchecked excerpt would deliver a
# blank body that reads like a unit which failed quietly. bootstrap.sh puts the service
# user in the systemd-journal group for this reason.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALERT="${HERE}/alert.sh"

JOURNAL_LINES="${TRADINGSYS_ALERT_JOURNAL_LINES:-30}"

UNIT="${1:-}"
if [[ -z "$UNIT" ]]; then
    echo "usage: alert_unit_failed.sh UNIT" >&2
    exit 2
fi

RESULT="$(systemctl show "$UNIT" -p Result --value 2>/dev/null)"
EXIT_STATUS="$(systemctl show "$UNIT" -p ExecMainStatus --value 2>/dev/null)"
STATE="$(systemctl is-active "$UNIT" 2>/dev/null)"

EXCERPT="$(journalctl -u "$UNIT" -n "$JOURNAL_LINES" --no-pager -o cat 2>&1)"
if [[ -z "${EXCERPT//[[:space:]]/}" ]]; then
    EXCERPT="(no journal output for ${UNIT}. If this host is otherwise working, the
service user cannot read other units' journals: re-run deploy/provision/bootstrap.sh,
which adds it to the systemd-journal group.)"
fi

BODY="$(printf 'result: %s\nexit status: %s\nstate: %s\n\n%s' \
    "${RESULT:-unknown}" "${EXIT_STATUS:-unknown}" "${STATE:-unknown}" "$EXCERPT")"

exec "$ALERT" raise "unit:${UNIT}" "tradingsys: ${UNIT} failed" "$BODY"
