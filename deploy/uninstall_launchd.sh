#!/bin/bash
set -euo pipefail
for label in com.derek.spxnoon com.derek.spxnoon.wrap; do
  DST="$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootout "gui/$(id -u)" "$DST" 2>/dev/null || true
  rm -f "$DST"
  echo "removed $DST"
done
