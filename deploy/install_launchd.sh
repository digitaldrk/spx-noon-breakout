#!/bin/bash
# Installs the weekday 11:45 session and the 16:20 read-only note. Undo with uninstall_launchd.sh.
#
#   ./install_launchd.sh        paper/dry session (com.derek.spxnoon) + wrap (com.derek.spxnoon.wrap)
#   ./install_launchd.sh live   REAL MONEY session (com.derek.spxnoon.live) + live wrap
#                               (com.derek.spxnoon.live.wrap). New labels only. It does not
#                               bootout or rewrite the paper labels.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/Library/Logs/spx-noon-breakout" "$HOME/Library/LaunchAgents" \
  "$HOME/Documents/spx-noon-breakout/state" "$HOME/Documents/spx-noon-breakout/reports"
install_one() {
  local src="$1" dst="$2" label="$3" account="${4:-}"
  sed "s#__HOME__#$HOME#g; s#__LIVE_ACCOUNT__#$account#g" "$src" > "$dst"
  # The live plists carry an account id, so keep them to this user only.
  if [ -n "$account" ]; then chmod 600 "$dst"; fi
  plutil -lint "$dst"
  launchctl bootout "gui/$(id -u)" "$dst" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dst"
  echo "installed $dst"
  launchctl print "gui/$(id -u)/$label" | grep -E "state|next" || true
}

install_live() {
  local account="${SPX_NOON_LIVE_ACCOUNT:-}"
  if [ -z "$account" ]; then
    echo "refusing: set SPX_NOON_LIVE_ACCOUNT to the live account id before installing the live jobs" >&2
    exit 2
  fi
  case "$account" in
    D*)
      echo "refusing: SPX_NOON_LIVE_ACCOUNT starts with D, which is an IBKR paper account" >&2
      exit 2
      ;;
    *[!A-Za-z0-9]*)
      echo "refusing: SPX_NOON_LIVE_ACCOUNT is not a plain IBKR account id" >&2
      exit 2
      ;;
  esac
  install_one "$DIR/com.derek.spxnoon.live.plist.template" \
    "$HOME/Library/LaunchAgents/com.derek.spxnoon.live.plist" "com.derek.spxnoon.live" "$account"
  install_one "$DIR/com.derek.spxnoon.live.wrap.plist.template" \
    "$HOME/Library/LaunchAgents/com.derek.spxnoon.live.wrap.plist" "com.derek.spxnoon.live.wrap" "$account"
  echo
  echo "WARNING: these are REAL MONEY jobs (run.sh live / run_wrap.sh live)."
  echo "WARNING: this script did NOT disable the paper job (com.derek.spxnoon), Candle, or Fly."
  echo "WARNING: turn those off yourself before a live session, and keep TWS or IB Gateway"
  echo "WARNING: logged into the live account with the API on and Read-Only API off."
  echo "Set config.json port to 7496 (TWS) or 4001 (Gateway) and account to this same id."
  echo "This job passes mode live, so leave config mode dry if bare ./run.sh should stay dry."
}

case "${1:-}" in
  "") ;;
  live) install_live; exit 0 ;;
  *) echo "usage: install_launchd.sh [live]" >&2; exit 2 ;;
esac

install_one "$DIR/com.derek.spxnoon.plist.template" \
  "$HOME/Library/LaunchAgents/com.derek.spxnoon.plist" "com.derek.spxnoon"
install_one "$DIR/com.derek.spxnoon.wrap.plist.template" \
  "$HOME/Library/LaunchAgents/com.derek.spxnoon.wrap.plist" "com.derek.spxnoon.wrap"
