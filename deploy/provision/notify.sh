#!/usr/bin/env bash
#
# Send one alert to Telegram. The last hop of the alerting path, and deliberately the
# least clever thing in it: it sends exactly one message and knows nothing about whether
# that message is a repeat, a recovery, or the first anyone has heard of a problem. That
# knowledge lives in alert.sh, one layer up.
#
# Reads its credentials from the environment, which the systemd units supply from
# /opt/tradingsys/.env. They are secrets and live beside the database password; nothing
# here is committed.
#
#   TRADINGSYS_ALERT_TELEGRAM_TOKEN     bot token from BotFather
#   TRADINGSYS_ALERT_TELEGRAM_CHAT_ID   chat to deliver to
#   TRADINGSYS_ALERT_TELEGRAM_API       API base, defaulting to the real one
#
# The API base is a variable rather than a constant for two reasons. A hardcoded
# endpoint is a hardcoded value, and this is the one script in the repository whose
# whole job happens outside the machine, so the only way to test it is to point it at a
# server that answers.
#
# WHAT BREAKS IF THIS FAILS QUIETLY: an alerter that fails silently is strictly worse
# than none, because it converts "you are not being told about problems" into "there are
# no problems". Every failure path here logs loudly to stderr, which on a systemd unit
# means journald, and exits non-zero.

set -uo pipefail

usage() { echo "usage: notify.sh SUBJECT [BODY]" >&2; exit 2; }

SUBJECT="${1:-}"
BODY="${2:-}"
[[ -n "$SUBJECT" ]] || usage

TOKEN="${TRADINGSYS_ALERT_TELEGRAM_TOKEN:-}"
CHAT_ID="${TRADINGSYS_ALERT_TELEGRAM_CHAT_ID:-}"
API_BASE="${TRADINGSYS_ALERT_TELEGRAM_API:-https://api.telegram.org}"

if [[ -z "$TOKEN" || -z "$CHAT_ID" ]]; then
    echo "alerting is not configured: TRADINGSYS_ALERT_TELEGRAM_TOKEN and" >&2
    echo "TRADINGSYS_ALERT_TELEGRAM_CHAT_ID must both be set in .env. The condition that" >&2
    echo "triggered this alert is real and has not been delivered anywhere." >&2
    echo "SUBJECT: ${SUBJECT}" >&2
    [[ -n "$BODY" ]] && echo "BODY: ${BODY}" >&2
    exit 1
fi

HOST="$(hostname)"
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# Telegram caps a message at 4096 characters. Truncated here rather than by the API,
# which would reject the whole message and lose the subject with it.
TEXT="$(printf '%s\n\nhost: %s\ntime: %s\n\n%s' "$SUBJECT" "$HOST" "$STAMP" "$BODY" | head -c 3800)"

RESPONSE="$(curl -sS --max-time 20 --retry 3 --retry-delay 5 --retry-connrefused \
    -X POST "${API_BASE}/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${TEXT}" \
    -w '\n%{http_code}' 2>&1)"
STATUS="$?"
CODE="$(tail -1 <<<"$RESPONSE")"

if [[ "$STATUS" -ne 0 ]]; then
    # DNS failure, connection refused, timeout. Named separately from an HTTP error
    # because the remedies differ and this host's networking has been unreliable before.
    echo "alert delivery failed at the transport: curl exited ${STATUS}" >&2
    echo "${RESPONSE}" >&2
    echo "UNDELIVERED SUBJECT: ${SUBJECT}" >&2
    exit 1
fi
if [[ "$CODE" != "200" ]]; then
    # A wrong token gives 401, a wrong chat id 400. Both are configuration and both are
    # invisible unless said out loud.
    echo "alert delivery refused by Telegram: HTTP ${CODE}" >&2
    head -c 500 <<<"$RESPONSE" >&2
    echo >&2
    echo "UNDELIVERED SUBJECT: ${SUBJECT}" >&2
    exit 1
fi

echo "alert delivered: ${SUBJECT}"
