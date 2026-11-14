#!/bin/bash
# Noon Breakout launcher (launchd runs this at 11:45 AM ET on weekdays).
# One job. config.json picks paper or live; the 11:45 job always runs it bare.
#   ./run.sh          uses config.json mode (the example is dry)
#   ./run.sh paper    one-shot override: send to TWS paper
#   ./run.sh dry      one-shot override: force dry for this launch
#   ./run.sh live     one-shot override: REAL MONEY on IBKR. It still needs
#                     config port 7496/4001, a non-D account, and TWS logged
#                     into that live session, or the bot refuses to start.
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
case "${1:-}" in
  paper|dry|alerts|live) EXTRA=(--mode "$1") ;;
esac
echo "$(date) starting Noon Breakout (TZ $(date +%Z)) ${EXTRA[*]:-}" >> "$LOG"
exec /usr/bin/caffeinate -i "$BASE/.venv/bin/python" -m spx_noon --config "$BASE/config.json" ${EXTRA[@]+"${EXTRA[@]}"} >> "$LOG" 2>&1
