#!/bin/bash
# Installs the weekday 11:45 session and the 16:20 read-only note. Undo with uninstall_launchd.sh.
#
#   ./install_launchd.sh   com.derek.spxnoon (run.sh) + com.derek.spxnoon.wrap (run_wrap.sh)
#
# There is one job. Paper and live are two settings of it: edit "mode", "port", and
# "account" in config.json and log TWS into that session. This script takes no arguments.
# It also boots out and deletes the retired com.derek.spxnoon.live pair if an older install
# left it behind, so only the two labels above stay scheduled.
set -euo pipefail
if [ "$#" -gt 0 ]; then
  echo "usage: install_launchd.sh   (no arguments; config.json picks paper or live)" >&2
  exit 2
fi
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/Library/Logs/spx-noon-breakout" "$HOME/Library/LaunchAgents" \
  "$HOME/Documents/spx-noon-breakout/state" "$HOME/Documents/spx-noon-breakout/reports"
# An older install could have left the retired real-money pair bootstrapped. Nothing installs
# those labels any more, and leaving them scheduled would mean two 11:45 jobs on one client id.
for retired in com.derek.spxnoon.live com.derek.spxnoon.live.wrap; do
  old="$HOME/Library/LaunchAgents/$retired.plist"
  [ -e "$old" ] || continue
  launchctl bootout "gui/$(id -u)" "$old" 2>/dev/null || true
  rm -f "$old"
  echo "removed retired $old"
done

install_one() {
  local src="$1" dst="$2" label="$3"
  sed "s#__HOME__#$HOME#g" "$src" > "$dst"
  plutil -lint "$dst"
  launchctl bootout "gui/$(id -u)" "$dst" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dst"
  echo "installed $dst"
  launchctl print "gui/$(id -u)/$label" | grep -E "state|next" || true
}

install_one "$DIR/com.derek.spxnoon.plist.template" \
  "$HOME/Library/LaunchAgents/com.derek.spxnoon.plist" "com.derek.spxnoon"
install_one "$DIR/com.derek.spxnoon.wrap.plist.template" \
  "$HOME/Library/LaunchAgents/com.derek.spxnoon.wrap.plist" "com.derek.spxnoon.wrap"
