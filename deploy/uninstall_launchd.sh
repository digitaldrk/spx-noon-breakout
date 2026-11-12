#!/bin/bash
# Removes the paper/dry labels and, when present, the real-money live labels.
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
