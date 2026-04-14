#!/usr/bin/env bash
# notify-hubitat — standalone Hubitat push-notification helper for CI workflows.
#
# Lifted out of scripts/sync-watcher.py so it outlives the sync-watcher retirement.
# Reads config from $HUBITAT_CONFIG (default: /root/.config/scottycore-hubitat.json)
# which must contain { "url": "...", "access_token": "..." }.
#
# Usage:
#   notify-hubitat P2 "scottystrike PR #42 needs review: <url>"
#
# Priorities:
#   P1, P2 -> alarm=true  (audible alert)
#   P3, P4 -> alarm=false (silent push)
#
# Exit codes:
#   0 sent OK
#   1 config missing or malformed (non-fatal for callers; log and continue)
#   2 HTTP call failed
set -euo pipefail

PRIORITY="${1:-P4}"
MESSAGE="${2:-}"
CONFIG="${HUBITAT_CONFIG:-/root/.config/scottycore-hubitat.json}"

if [[ -z "$MESSAGE" ]]; then
    echo "usage: notify-hubitat <P1-P4> <message>" >&2
    exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
    echo "notify-hubitat: config missing at $CONFIG — skipping" >&2
    exit 1
fi

URL=$(jq -r '.url' "$CONFIG")
TOKEN=$(jq -r '.access_token' "$CONFIG")
if [[ -z "$URL" || "$URL" == "null" || -z "$TOKEN" || "$TOKEN" == "null" ]]; then
    echo "notify-hubitat: config missing url or access_token" >&2
    exit 1
fi

case "$PRIORITY" in
    P1|P2) ALARM=true ;;
    *)     ALARM=false ;;
esac

PAYLOAD=$(jq -n \
    --arg priority "$PRIORITY" \
    --arg message "CLAUDE: $MESSAGE" \
    --argjson alarm "$ALARM" \
    '{priority: $priority, destination: "text", alarm: $alarm, message: $message}')

RESPONSE=$(curl -sS --max-time 15 \
    -H "Content-Type: application/json" \
    --data "$PAYLOAD" \
    "${URL}?access_token=${TOKEN}" || true)

if [[ "$RESPONSE" == *'"result":"OK"'* ]]; then
    echo "notify-hubitat: sent ($PRIORITY)"
    exit 0
fi

echo "notify-hubitat: failed: ${RESPONSE:0:200}" >&2
exit 2
