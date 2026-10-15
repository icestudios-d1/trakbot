#!/bin/bash
# Removes the Cyril agent from this Mac and says what it removed. Needs sudo on purpose (like the Windows uninstaller):
# a standard user cannot remove it.
#
#   curl -fsSL 'https://cyril.example.com/uninstall.sh' | sudo bash
#
# Removes: the agent job (LaunchAgent), the daily updater job (LaunchDaemon), the running process and
# /Library/Application Support/Cyril (program, config + key, log, queued data). The macOS permissions the person
# granted (Screen Recording, Automation) stay listed in System Settings; they do nothing without the program.
# Data already stored on the server is not touched.
set -uo pipefail
R="${CYRIL_TEST_ROOT:-}"
APPDIR="$R/Library/Application Support/Cyril"
AGENT_LABEL="in.icestudios.cyril"; UPDATER_LABEL="in.icestudios.cyril.updater"
AGENT_PLIST="$R/Library/LaunchAgents/$AGENT_LABEL.plist"; UPDATER_PLIST="$R/Library/LaunchDaemons/$UPDATER_LABEL.plist"
FAILURES=0
row() { if [ "$2" = ok ]; then printf '  \033[32m[ok]\033[0m %s%s\n' "$1" "${3:+ - $3}"; else printf '  \033[33m[!!]\033[0m %s%s\n' "$1" "${3:+ - $3}"; FAILURES=$((FAILURES + 1)); fi; }

printf '\nCyril agent uninstaller (macOS)\n'
if [ -z "$R" ]; then
  [ "$(uname -s)" = "Darwin" ] || { echo "  [xx] This is the macOS uninstaller." >&2; exit 1; }
  [ "$(id -u)" -eq 0 ] || { echo "  [xx] Run it with sudo:  curl -fsSL '<address>/uninstall.sh' | sudo bash" >&2; exit 1; }
fi

# jobs first, so nothing can restart the process we are about to stop
if [ -z "$R" ]; then
  launchctl bootout system "$UPDATER_PLIST" 2>/dev/null
  for uid in $(ps -axo uid= -o comm= | awk '$2 ~ /loginwindow$/ {print $1}' | sort -u); do
    launchctl bootout "gui/$uid" "$AGENT_PLIST" 2>/dev/null
  done
fi
for f in "$UPDATER_PLIST" "$AGENT_PLIST"; do
  if [ -e "$f" ]; then rm -f "$f" && row "$f" ok "removed" || row "$f" bad "could not remove"; else row "$f" ok "was not present"; fi
done

if [ -z "$R" ] && pgrep -x cyril >/dev/null 2>&1; then
  pkill -x cyril; sleep 1
  if pgrep -x cyril >/dev/null 2>&1; then row "Agent process" bad "still running"; else row "Agent process" ok "stopped"; fi
else
  row "Agent process" ok "was not running"
fi

if [ -d "$APPDIR" ]; then rm -rf "$APPDIR" && row "$APPDIR" ok "deleted" || row "$APPDIR" bad "could not delete"; else row "$APPDIR" ok "was not present"; fi

echo
if [ "$FAILURES" -gt 0 ]; then printf '\033[33mCyril agent partly removed: %s item(s) need attention (see above).\033[0m\n' "$FAILURES"
else printf '\033[32mCyril agent removed. Nothing is left running on this Mac.\033[0m\n'; fi
echo "Its past activity stays on the server until you delete it there."
echo
