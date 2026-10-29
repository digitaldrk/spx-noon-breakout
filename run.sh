#!/bin/bash
# Noon Breakout launcher (launchd runs this at 11:45 AM ET on weekdays).
#   ./run.sh          uses config.json mode (the example is dry)
#   ./run.sh paper    send to TWS paper
#   ./run.sh dry      force dry for this launch
set -euo pipefail
BASE="${SPX_NOON_HOME:-$HOME/Documents/spx-noon-breakout}"
cd "$BASE"
mkdir -p "$BASE/state" "$BASE/logs"
LOG="$BASE/state/run_$(date +%F).log"
DOW=$(date +%u)
if [ "$DOW" -gt 5 ]; then
  echo "$(date) weekend - not running" >> "$LOG"
  exit 0
fi
EXTRA=()
if [ "${1:-}" = "paper" ] || [ "${1:-}" = "dry" ] || [ "${1:-}" = "alerts" ]; then
  EXTRA=(--mode "$1")
fi
echo "$(date) starting Noon Breakout (TZ $(date +%Z)) ${EXTRA[*]:-}" >> "$LOG"
exec /usr/bin/caffeinate -i "$BASE/.venv/bin/python" -m spx_noon --config "$BASE/config.json" "${EXTRA[@]}" >> "$LOG" 2>&1
