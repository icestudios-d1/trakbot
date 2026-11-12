#!/bin/bash
# Removes the NetMon agent from this Mac (and the old Cyril agent too, if this Mac still has it) and says what it
# removed. Needs sudo on purpose (like the Windows uninstaller): a standard user cannot remove it.
#
#   curl -fsSL 'https://your-server.example.com/uninstall.sh' | sudo bash
#
# Removes: the agent job (LaunchAgent), the daily updater job (LaunchDaemon), the running process and
# /Library/Application Support/NetMon (program, config + key, log, queued data), and the same for the old Cyril
# names. The macOS permissions the person granted (Screen Recording, Automation) stay listed in System Settings;
# they do nothing without the program.
# Data already stored on the server is not touched.
set -uo pipefail
R="${NETMON_TEST_ROOT:-${CYRIL_TEST_ROOT:-}}"
APPDIRS=("$R/Library/Application Support/NetMon" "$R/Library/Application Support/Cyril")
AGENT_LABELS=("in.icestudios.netmon" "in.icestudios.cyril")
UPDATER_LABELS=("in.icestudios.netmon.updater" "in.icestudios.cyril.updater")
# the plists, and the ".disabled" copies a hand-over from Cyril leaves behind
PLISTS=()
for l in "${AGENT_LABELS[@]}"; do PLISTS+=("$R/Library/LaunchAgents/$l.plist" "$R/Library/LaunchAgents/$l.plist.disabled"); done
for l in "${UPDATER_LABELS[@]}"; do PLISTS+=("$R/Library/LaunchDaemons/$l.plist" "$R/Library/LaunchDaemons/$l.plist.disabled"); done
FAILURES=0
row() { if [ "$2" = ok ]; then printf '  \033[32m[ok]\033[0m %s%s\n' "$1" "${3:+ - $3}"; else printf '  \033[33m[!!]\033[0m %s%s\n' "$1" "${3:+ - $3}"; FAILURES=$((FAILURES + 1)); fi; }

printf '\nNetMon agent uninstaller (macOS)\n'
if [ -z "$R" ]; then
  [ "$(uname -s)" = "Darwin" ] || { echo "  [xx] This is the macOS uninstaller." >&2; exit 1; }
  [ "$(id -u)" -eq 0 ] || { echo "  [xx] Run it with sudo:  curl -fsSL '<address>/uninstall.sh' | sudo bash" >&2; exit 1; }
fi

# jobs first, so nothing can restart the process we are about to stop
if [ -z "$R" ]; then
  for l in "${UPDATER_LABELS[@]}"; do launchctl bootout "system/$l" 2>/dev/null; done
  for uid in $(ps -axo uid= -o comm= | awk '$2 ~ /loginwindow$/ {print $1}' | sort -u); do
    for l in "${AGENT_LABELS[@]}"; do launchctl bootout "gui/$uid/$l" 2>/dev/null; done
  done
fi
for f in "${PLISTS[@]}"; do
  if [ -e "$f" ]; then rm -f "$f" && row "$f" ok "removed" || row "$f" bad "could not remove"; else row "$f" ok "was not present"; fi
done

running() { pgrep -x netmon >/dev/null 2>&1 || pgrep -x cyril >/dev/null 2>&1; }
if [ -z "$R" ] && running; then
  pkill -x netmon; pkill -x cyril; sleep 1
  if running; then row "Agent process" bad "still running"; else row "Agent process" ok "stopped"; fi
else
  row "Agent process" ok "was not running"
fi

for APPDIR in "${APPDIRS[@]}"; do
  if [ -d "$APPDIR" ]; then rm -rf "$APPDIR" && row "$APPDIR" ok "deleted" || row "$APPDIR" bad "could not delete"; else row "$APPDIR" ok "was not present"; fi
done

echo
if [ "$FAILURES" -gt 0 ]; then printf '\033[33mNetMon agent partly removed: %s item(s) need attention (see above).\033[0m\n' "$FAILURES"
else printf '\033[32mNetMon agent removed. Nothing is left running on this Mac.\033[0m\n'; fi
echo "Its past activity stays on the server until you delete it there."
echo
