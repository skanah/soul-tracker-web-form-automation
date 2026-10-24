#!/usr/bin/env bash
# Long-running loop version (local use). On cPanel prefer site_watcher_once.sh
# + a 5-min cron entry instead of an infinite loop.
# Watches the SoulTracker API and auto-starts the run when it returns to life.
LOG="data/site_watcher.log"
API="https://api.christembassysoultracker.org/config/app"
DASH="${DASH_URL:-http://127.0.0.1:5000}"
while true; do
  ts=$(date '+%F %T')
  code=$(curl -s -o /tmp/w_api.json -w '%{http_code}' --max-time 20 "$API" 2>/dev/null)
  if [ "$code" = "200" ] && grep -qE '^[[:space:]]*\{' /tmp/w_api.json 2>/dev/null; then
    live=$(curl -s --max-time 8 "$DASH/status" 2>/dev/null | grep -o '"live": *true' | head -1)
    if [ -z "$live" ]; then
      resp=$(curl -s --max-time 20 -X POST -H 'Content-Type: application/json' -d '{"mode":"add"}' "$DASH/run" 2>/dev/null | head -c 120)
      echo "$ts API IS BACK -> triggered run: $resp" >> "$LOG"
    else
      echo "$ts API up, worker already live" >> "$LOG"
    fi
  else
    echo "$ts api=$code (waiting)" >> "$LOG"
  fi
  sleep 300
done
