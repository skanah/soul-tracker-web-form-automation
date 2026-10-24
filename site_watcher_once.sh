#!/usr/bin/env bash
# Cron-safe single pass: if the real SoulTracker API is back AND no worker is
# live, trigger the run. Add to cPanel Cron Jobs every 5 minutes:
#
#   bash $HOME/soultracker/site_watcher_once.sh
#
# (set DASH_URL if your app lives at a non-default address, e.g.
#   DASH_URL=https://yourdomain.com/soultracker bash .../site_watcher_once.sh)
DASH="${DASH_URL:-http://127.0.0.1:5000}"
API="${API_URL:-https://api.christembassysoultracker.org/config/app}"
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/data/site_watcher.log"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

ts=$(date '+%F %T')
code=$(curl -s -o "$TMP" -w '%{http_code}' --max-time 20 "$API" 2>/dev/null)
if [ "$code" = "200" ] && grep -qE '^[[:space:]]*\{' "$TMP" 2>/dev/null; then
  live=$(curl -s --max-time 8 "$DASH/status" 2>/dev/null | grep -o '"live": *true' | head -1)
  if [ -z "$live" ]; then
    resp=$(curl -s --max-time 20 -X POST -H 'Content-Type: application/json' \
      -d '{"mode":"add"}' "$DASH/run" 2>/dev/null | head -c 160)
    echo "$ts API IS BACK -> triggered run: $resp" >> "$LOG"
  else
    echo "$ts API up, worker already live" >> "$LOG"
  fi
else
  echo "$ts api=$code (waiting)" >> "$LOG"
fi
