#!/bin/bash
# Removes the two Noon Breakout labels, plus the retired com.derek.spxnoon.live pair
# when an older install left those plists on disk. Nothing installs that pair any more.
set -euo pipefail
for label in com.derek.spxnoon com.derek.spxnoon.wrap \
             com.derek.spxnoon.live com.derek.spxnoon.live.wrap; do
  DST="$HOME/Library/LaunchAgents/$label.plist"
  if [ ! -e "$DST" ]; then
    continue
  fi
  launchctl bootout "gui/$(id -u)" "$DST" 2>/dev/null || true
  rm -f "$DST"
  echo "removed $DST"
done
