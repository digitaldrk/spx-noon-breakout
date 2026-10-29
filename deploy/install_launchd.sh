#!/bin/bash
# Installs the weekday 11:45 session and the 16:20 read-only note. Undo with uninstall_launchd.sh.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/Library/Logs/spx-noon-breakout" "$HOME/Library/LaunchAgents" \
  "$HOME/Documents/spx-noon-breakout/state" "$HOME/Documents/spx-noon-breakout/reports"
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
