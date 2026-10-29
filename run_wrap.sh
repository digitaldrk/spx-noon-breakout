#!/bin/bash
# Read-only end-of-day note. Weekdays 4:20 PM ET via launchd. Does not place orders.
set -euo pipefail
BASE="${SPX_NOON_HOME:-$HOME/Documents/spx-noon-breakout}"
cd "$BASE"
export SPX_NOON_HOME="$BASE"
mkdir -p "$BASE/state" "$BASE/reports"
exec /usr/bin/caffeinate -i "$BASE/.venv/bin/python" -m spx_noon wrap "$@"
