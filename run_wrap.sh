#!/bin/bash
# Read-only end-of-day note. Weekdays 4:20 PM ET via launchd. Does not place orders.
#   ./run_wrap.sh         paper book: state/trades.csv  -> reports/wrap-<date>.md
#   ./run_wrap.sh live    live book:  state/trades-live.csv -> reports/wrap-live-<date>.md
set -euo pipefail
BASE="${SPX_NOON_HOME:-$HOME/Documents/spx-noon-breakout}"
cd "$BASE"
export SPX_NOON_HOME="$BASE"
mkdir -p "$BASE/state" "$BASE/reports"
BOOK=()
case "${1:-}" in
  live|paper) BOOK=(--book "$1"); shift ;;
esac
exec /usr/bin/caffeinate -i "$BASE/.venv/bin/python" -m spx_noon wrap ${BOOK[@]+"${BOOK[@]}"} "$@"
