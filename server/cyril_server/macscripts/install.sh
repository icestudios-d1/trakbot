#!/bin/bash
# NetMon agent installer for macOS (formerly "Cyril"). Needs only what macOS ships with (bash, curl, launchctl): no
# Homebrew, no Python. Safe to run again: it updates an existing install in place.
#
# A Mac that still has the old Cyril agent is moved over by this same script, with no step for the user (the old
# Cyril updater job fetches this file like any other update). The hand-over: download and self-test the new program
# FIRST; only then stop the Cyril agent, copy its config and unsent data into the NetMon folder, start NetMon and
# check that it is running. If anything fails, the Cyril agent is switched back on and nothing is lost. The old
# Cyril folder and jobs are only switched off here (plists renamed to .disabled), never deleted: removing them is a
# separate, later update. One thing no installer can do: macOS ties Screen Recording to the program, so the person
# at the Mac may be asked to allow "netmon" once (the dashboard shows which Macs still need it).
#
# The easiest way is the one-liner on the dashboard's "Install agents" page (the server fills in its own address
# and the agent key):
#
#   curl -fsSL 'https://your-server.example.com/install.sh?key=AGENT_KEY' | sudo bash
#
# Options (after "bash -s --"):  --user "Priya"   report under this name instead of the Mac login
#                                --silent         quiet install for roll-outs (MDM, Jamf, a script): no step list, no first-report
#                                                 wait, one line at the end; failures still exit non-zero
#                                --unattended     the same with no final line at all (used by the daily updater)
#
# How it runs: the agent must live inside the logged-in user's session (that is where the windows and the screen
# are), so it is a LaunchAgent that starts at login and is restarted by launchd if it is closed. A second job, a
# LaunchDaemon running as root, re-runs this installer daily and at every start so new builds reach every Mac.
# Testing: set NETMON_TEST_ROOT to prefix every path (the tests run this against a scratch directory).
set -euo pipefail

SERVER_URL='{{SERVER_URL}}'
AGENT_KEY='{{AGENT_KEY}}'
PAYLOAD_KEY='{{PAYLOAD_KEY}}'   # the server's PUBLIC key: the agent seals everything to it and cannot read it back
USER_NAME=""
UNATTENDED=0
SILENT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="${2:-}"; shift 2 ;;
    --unattended) UNATTENDED=1; shift ;;
    --silent) UNATTENDED=1; SILENT=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

R="${NETMON_TEST_ROOT:-${CYRIL_TEST_ROOT:-}}"
APPDIR="$R/Library/Application Support/NetMon"
BIN="$APPDIR/netmon"
PROC="netmon"
CFG="$APPDIR/config.json"
DATA="$APPDIR/data"
AGENT_LABEL="in.icestudios.netmon"
UPDATER_LABEL="in.icestudios.netmon.updater"
AGENT_PLIST="$R/Library/LaunchAgents/$AGENT_LABEL.plist"
UPDATER_PLIST="$R/Library/LaunchDaemons/$UPDATER_LABEL.plist"
MARKER="$APPDIR/migrated-from-cyril"      # written once the hand-over from Cyril has succeeded
# What the old product left on a Mac. Only ever switched off here (see the header), never deleted.
OLD_APPDIR="$R/Library/Application Support/Cyril"
OLD_CFG="$OLD_APPDIR/config.json"
OLD_DATA="$OLD_APPDIR/data"
OLD_PROC="cyril"
OLD_AGENT_LABEL="in.icestudios.cyril"
OLD_UPDATER_LABEL="in.icestudios.cyril.updater"
OLD_AGENT_PLIST="$R/Library/LaunchAgents/$OLD_AGENT_LABEL.plist"
OLD_UPDATER_PLIST="$R/Library/LaunchDaemons/$OLD_UPDATER_LABEL.plist"

say()  { [ "$UNATTENDED" -eq 1 ] || printf '  \033[36m[..]\033[0m %s\n' "$1"; }
ok()   { [ "$UNATTENDED" -eq 1 ] || printf '  \033[32m[ok]\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m[!!]\033[0m %s\n' "$1"; }
fail() { printf '\n  \033[31m[xx]\033[0m %s\n\n' "$1" >&2; exit 1; }

[ "$UNATTENDED" -eq 1 ] || printf '\nWelcome to NetMon by ICE\n'

# 1. Preconditions ----------------------------------------------------------------------------------------------
[ -n "$R" ] || [ "$(uname -s)" = "Darwin" ] || fail "This installer is for macOS. On Windows use the PowerShell command."
[ "$(id -u)" -eq 0 ] || fail "Run it with sudo:  curl -fsSL '<address>/install.sh?key=...' | sudo bash"
SERVER_URL="${SERVER_URL%/}"
[ -n "$SERVER_URL" ] || fail "No server address. Use the command from the dashboard's Install agents page."
[ -n "$AGENT_KEY" ] || fail "No agent key. Use the command from the dashboard's Install agents page (it includes ?key=...)."
case "$(uname -m)" in
  arm64|aarch64) ARCH=arm64 ;;
  x86_64) ARCH=x86_64 ;;
  *) fail "Unsupported CPU: $(uname -m)" ;;
esac
CONSOLE_UID="$(stat -f%u /dev/console 2>/dev/null || echo 0)"   # whoever is sitting at the Mac (0 = login window)

# Is the old Cyril agent still on this Mac and not yet handed over?
OLD_PRESENT=0
if [ -d "$OLD_APPDIR" ] || [ -e "$OLD_AGENT_PLIST" ] || [ -e "$OLD_AGENT_PLIST.disabled" ]; then OLD_PRESENT=1; fi
MIGRATING=0
if [ "$OLD_PRESENT" -eq 1 ] && [ ! -f "$MARKER" ]; then MIGRATING=1; fi

# 2. The server and the published Mac build ------------------------------------------------------------------------
say "Contacting $SERVER_URL"
curl -fsS -m 15 "$SERVER_URL/api/health" >/dev/null || fail "Cannot reach $SERVER_URL. Check the address and this Mac's network."
ok "Server reachable"

MANIFEST="$(curl -fsS -m 15 "$SERVER_URL/api/agent/mac-manifest?arch=$ARCH" 2>/dev/null)" \
  || fail "No Mac ($ARCH) build is published on the server yet. On a $ARCH Mac run mac/build.sh from the repo (it writes into server/dist)."
field() { printf '%s\n' "$MANIFEST" | sed -n "s/^$1=//p" | head -1 || true; }
M_URL="$(field url)"; M_SHA="$(field sha256)"; M_BUILD="$(field build)"; M_VERSION="$(field version)"; M_SELFTEST="$(field selftest)"
[ -n "$M_URL" ] || fail "The server's Mac manifest is incomplete."
INFO="version $M_VERSION${M_BUILD:+, build $M_BUILD}"
[ -n "$M_SHA" ] || warn "The server publishes no checksum for the Mac agent, so the download cannot be verified."

# 3. Download first; only stop the running agent once the new file is verified ------------------------------------
NEW_BUILD_STAMP="${M_BUILD:-${M_SHA:0:12}}"
CURRENT_SHA=""
if [ -x "$BIN" ]; then CURRENT_SHA="$(shasum -a 256 "$BIN" | cut -d' ' -f1 || true)"; fi
mkdir -p "$APPDIR"
TMP="$(mktemp "${TMPDIR:-/tmp}/netmon.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
if [ -n "$M_SHA" ] && [ "$CURRENT_SHA" = "$M_SHA" ]; then
  ok "Agent is already the published build ($INFO)"
  UP_TO_DATE=1
else
  UP_TO_DATE=0
  say "Downloading"
  curl -fsSL -m 300 -o "$TMP" "$SERVER_URL$M_URL?h=${M_SHA:0:16}" || fail "Could not download $SERVER_URL$M_URL. The build may not be published."
  if [ -n "$M_SHA" ]; then
    [ "$(shasum -a 256 "$TMP" | cut -d' ' -f1)" = "$M_SHA" ] || fail "Downloaded file does not match the published checksum. Nothing was installed."
    ok "Checksum verified"
  fi
  chmod 755 "$TMP"
  if [ "$M_SELFTEST" = "1" ]; then
    "$TMP" --selftest || fail "The downloaded agent does not start (self-test failed). It is a broken build: rebuild with mac/build.sh and run this again. Nothing was installed."
    ok "Start Successful"
  fi
fi

# 4. Install -----------------------------------------------------------------------------------------------------------
if [ "$UP_TO_DATE" -eq 0 ]; then
  if [ -x "$BIN" ]; then
    if [ -z "$R" ]; then launchctl bootout "gui/$CONSOLE_UID/$AGENT_LABEL" 2>/dev/null || true; pkill -x "$PROC" 2>/dev/null || true; sleep 1; fi
  fi
  install -m 755 "$TMP" "$BIN"
  ok "System Installed to $APPDIR ($INFO)"
fi

# config: readable by every user (the agent runs as the logged-in user and must read it); only root can change it
OLD_USER=""
for f in "$CFG" "$OLD_CFG"; do   # this install's own config first, else the one the Cyril agent had
  if [ -z "$OLD_USER" ] && [ -f "$f" ]; then OLD_USER="$(sed -n 's/.*"user" *: *"\([^"]*\)".*/\1/p' "$f" | head -1 || true)"; fi
done
[ -n "$USER_NAME" ] || USER_NAME="$OLD_USER"
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
{
  printf '{\n  "server": "%s",\n  "key": "%s"' "$(esc "$SERVER_URL")" "$(esc "$AGENT_KEY")"
  if [ -n "$USER_NAME" ]; then printf ',\n  "user": "%s"' "$(esc "$USER_NAME")"; fi
  case "$PAYLOAD_KEY" in ""|*[{]*) ;; *) printf ',\n  "payload_key": "%s"' "$(esc "$PAYLOAD_KEY")" ;; esac
  printf '\n}\n'
} > "$CFG"
if [ -n "$NEW_BUILD_STAMP" ]; then printf '%s' "$NEW_BUILD_STAMP" > "$APPDIR/build.txt"; fi
mkdir -p "$DATA"
if [ -z "$R" ]; then
  # Only the program and its config belong to root. NEVER chown what is inside data/: the agent runs as the logged-in
  # user and must keep writing its log and its upload queue there. (An earlier installer chowned the whole
  # folder recursively: from then on the agent could neither queue an event nor write a log line, so the Mac went quiet.)
  chown root:wheel "$APPDIR" "$BIN" "$CFG"; chmod 755 "$APPDIR"; chmod 644 "$CFG"
  if [ -f "$APPDIR/build.txt" ]; then chown root:wheel "$APPDIR/build.txt"; chmod 644 "$APPDIR/build.txt"; fi
  chown root:wheel "$DATA"; chmod 1777 "$DATA"   # the agent's log and queued data; written by whoever is logged in
  # Repair a data folder that earlier installer took over: give root-owned files back to the person who uses this Mac.
  OWNER_UID="$CONSOLE_UID"
  if [ "$OWNER_UID" -eq 0 ]; then   # nobody at the Mac (the updater at start-up): the last person who logged in
    LAST_USER="$(defaults read /Library/Preferences/com.apple.loginwindow lastUserName 2>/dev/null || true)"
    if [ -n "$LAST_USER" ]; then OWNER_UID="$(id -u "$LAST_USER" 2>/dev/null || echo 0)"; fi
  fi
  if [ "$OWNER_UID" -ne 0 ]; then
    find "$DATA" -mindepth 1 -user 0 ! -name updater.log -exec chown "$OWNER_UID" {} + 2>/dev/null || true
  fi
fi
ok "Config written"

# 4b. Hand over from Cyril (step 1 of 2: stop the old agent, copy what it had not sent yet) ---------------------------
get_device() {
  local d=""
  d="$(ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null | awk -F'"' '/IOPlatformUUID/{print $4}' || true)"
  [ -n "$d" ] || d="$(hostname)"
  printf '%s' "$d"
}
report_migration() {   # $1 = 1 or 0 (worked), $2 = what happened. Shows in the dashboard's update log; never blocks anything.
  local okjson=false who="${USER_NAME:-$(hostname)}"
  [ "$1" = 1 ] && okjson=true
  curl -fsS -m 15 -X POST -H "X-Api-Key: $AGENT_KEY" -H 'Content-Type: application/json' \
    -d "$(printf '{"user":"%s","device":"%s","from_version":"cyril","to_version":"netmon %s","ok":%s,"detail":"%s"}' \
          "$(esc "$who")" "$(esc "$(get_device)")" "$(esc "$M_VERSION")" "$okjson" "$(esc "${2:0:1900}")")" \
    "$SERVER_URL/api/agent/update-log" >/dev/null 2>&1 || true
}
login_uids() { ps -axo uid= -o comm= | awk '$2 ~ /loginwindow$/ {print $1}' | sort -u; }
COPIED=0
if [ "$MIGRATING" -eq 1 ]; then
  say "Moving this Mac from Cyril to NetMon"
  if [ -z "$R" ]; then
    for uid in $(login_uids); do launchctl bootout "gui/$uid/$OLD_AGENT_LABEL" 2>/dev/null || true; done
    pkill -x "$OLD_PROC" 2>/dev/null || true
    sleep 2
    if pgrep -x "$OLD_PROC" >/dev/null 2>&1; then fail "The Cyril agent would not stop, so NetMon did not take over. Cyril is still monitoring this Mac; the next update will try again."; fi
  fi
  # keep the old agent from loading again at the next login: renamed, so it can be put back
  if [ -f "$OLD_AGENT_PLIST" ]; then mv "$OLD_AGENT_PLIST" "$OLD_AGENT_PLIST.disabled"; fi
  ok "Cyril agent stopped"
  # Unsent events and screenshots waiting for the server. Copied (not moved) so Cyril still has them if we roll back.
  # health.json is the old agent's own status, updater.log belongs to the old updater job.
  if [ -d "$OLD_DATA" ]; then
    for f in "$OLD_DATA"/* "$OLD_DATA"/.[!.]*; do
      [ -e "$f" ] || continue
      b="$(basename "$f")"
      case "$b" in health.json|updater.log) continue ;; esac
      if [ ! -e "$DATA/$b" ]; then cp -Rp "$f" "$DATA/$b" && COPIED=$((COPIED + 1)); fi
    done
  fi
  ok "Unsent data copied to $DATA ($COPIED item(s))"
fi
restore_cyril() {   # NetMon did not come up: switch the old agent straight back on
  warn "NetMon could not take over ($1). Switching the Cyril agent back on; the next update will try again."
  if [ -z "$R" ]; then
    for uid in $(login_uids); do launchctl bootout "gui/$uid/$AGENT_LABEL" 2>/dev/null || true; done
    pkill -x "$PROC" 2>/dev/null || true
  fi
  rm -f "$AGENT_PLIST"
  if [ -f "$OLD_AGENT_PLIST.disabled" ]; then mv "$OLD_AGENT_PLIST.disabled" "$OLD_AGENT_PLIST"; fi
  if [ -z "$R" ] && [ "$CONSOLE_UID" -ne 0 ] && [ -f "$OLD_AGENT_PLIST" ]; then
    launchctl bootstrap "gui/$CONSOLE_UID" "$OLD_AGENT_PLIST" 2>/dev/null || true
    launchctl kickstart -k "gui/$CONSOLE_UID/$OLD_AGENT_LABEL" 2>/dev/null || true
  fi
  report_migration 0 "rolled back to Cyril: $1"
}

# updater script: re-runs this installer using the saved address and key
cat > "$APPDIR/update.sh" <<'UPD'
#!/bin/bash
# Run by the NetMon updater job (root). Does nothing unless a new build is published -- except a remote uninstall
# order, which it carries out: this root job can remove the program and the launchd jobs, which the user-level agent
# cannot. The agent itself has already gone quiet (final flush) when it saw the order on its own poll.
TR="${NETMON_TEST_ROOT:-${CYRIL_TEST_ROOT:-}}"
CFG="/Library/Application Support/NetMon/config.json"
[ -f "$TR$CFG" ] && CFG="$TR$CFG"
SERVER="$(sed -n 's/.*"server" *: *"\([^"]*\)".*/\1/p' "$CFG" | head -1)"
KEY="$(sed -n 's/.*"key" *: *"\([^"]*\)".*/\1/p' "$CFG" | head -1)"
[ -n "$SERVER" ] && [ -n "$KEY" ] || exit 1
if [ -z "$TR" ]; then
  DEV="$(ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null | awk -F'"' '/IOPlatformUUID/{print $4}')"; [ -n "$DEV" ] || DEV="$(hostname)"
  RM="$(curl -fsS -m 20 -H "X-Api-Key: $KEY" "$SERVER/api/agent/removal?device=$DEV" 2>/dev/null || true)"
  case "$RM" in *remove=1*)
    curl -fsSL -m 60 "$SERVER/uninstall.sh" | bash
    curl -fsS -m 20 -X POST -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
      -d "{\"device\":\"$DEV\",\"state\":\"removed\",\"detail\":\"removed by updater job\"}" \
      "$SERVER/api/agent/removal-state" >/dev/null 2>&1 || true
    exit 0 ;;
  esac
fi
curl -fsS -m 60 "$SERVER/install.sh?key=$KEY" | bash -s -- --unattended
UPD
chmod 755 "$APPDIR/update.sh"

# 5. launchd jobs ---------------------------------------------------------------------------------------------------------
mkdir -p "$(dirname "$AGENT_PLIST")" "$(dirname "$UPDATER_PLIST")"
cat > "$AGENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$AGENT_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Library/Application Support/NetMon/netmon</string>
    <string>--config</string><string>/Library/Application Support/NetMon/config.json</string>
    <string>--update-check-every</string><string>0</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict>
</plist>
PLIST
cat > "$UPDATER_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$UPDATER_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>/Library/Application Support/NetMon/update.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <key>ThrottleInterval</key><integer>600</integer>
  <key>StandardErrorPath</key><string>/Library/Application Support/NetMon/data/updater.log</string>
</dict>
</plist>
PLIST
if [ -z "$R" ]; then
  chown root:wheel "$AGENT_PLIST" "$UPDATER_PLIST"; chmod 644 "$AGENT_PLIST" "$UPDATER_PLIST"
fi
load_updater() {
  # Never unload the updater job when it is already loaded: this installer is usually running INSIDE that job
  # (update.sh), and unloading it kills this very script before it can load it again, leaving the Mac with no
  # updater at all. Its plist never changes between builds, so loading it once is enough.
  [ -z "$R" ] || return 0
  if ! launchctl print "system/$UPDATER_LABEL" >/dev/null 2>&1; then
    launchctl bootstrap system "$UPDATER_PLIST" 2>/dev/null \
      || warn "Could not load the updater job; run: sudo launchctl bootstrap system $UPDATER_PLIST"
  fi
}
# While handing over from Cyril the NetMon updater is only loaded once NetMon is confirmed running (below), so that a
# rolled-back attempt never leaves two updaters about; the old Cyril updater is the retry path until then.
if [ "$MIGRATING" -eq 0 ]; then load_updater; fi
ok "Updater job installed (daily ~03:00 and at every start; picks up new builds)"

SINCE="$(date +%s)"; SINCE=$((SINCE - 5))
if [ -z "$R" ]; then
  if [ "$UP_TO_DATE" -eq 1 ] && pgrep -x "$PROC" >/dev/null 2>&1; then
    ok "Agent is already running"
  elif [ "$CONSOLE_UID" -ne 0 ]; then
    launchctl bootout "gui/$CONSOLE_UID" "$AGENT_PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$CONSOLE_UID" "$AGENT_PLIST" || warn "Could not start the agent for the logged-in user; it will start at the next login."
    launchctl kickstart -k "gui/$CONSOLE_UID/$AGENT_LABEL" 2>/dev/null || true
    ok "Agent started for the logged-in user (it also starts at every login)"
  else
    warn "Nobody is logged in at the Mac right now; the agent will start at the next login."
  fi
fi

# 5b. Hand over from Cyril (step 2 of 2: check NetMon really runs, then switch the old updater off) ------------------
disable_old_updater() {
  # Never booted out: this installer may be running inside that very job. Its script is replaced by one that does
  # nothing (written to a new file and moved over, so a shell still reading the old one is not disturbed) and its
  # plist is renamed so it does not load again at the next start. Removing both is a later update.
  if [ -f "$OLD_APPDIR/update.sh" ] && ! grep -q 'handed over to NetMon' "$OLD_APPDIR/update.sh" 2>/dev/null; then
    printf '#!/bin/bash\n# handed over to NetMon: this old updater does nothing any more\nexit 0\n' > "$OLD_APPDIR/update.sh.new" \
      && chmod 755 "$OLD_APPDIR/update.sh.new" && mv -f "$OLD_APPDIR/update.sh.new" "$OLD_APPDIR/update.sh"
  fi
  if [ -f "$OLD_UPDATER_PLIST" ]; then mv "$OLD_UPDATER_PLIST" "$OLD_UPDATER_PLIST.disabled"; fi
}
if [ "$MIGRATING" -eq 1 ]; then
  if [ -z "$R" ] && [ "$CONSOLE_UID" -ne 0 ]; then
    UP=0
    for _ in $(seq 1 15); do sleep 2; if pgrep -x "$PROC" >/dev/null 2>&1; then UP=1; break; fi; done
    if [ "$UP" -eq 0 ]; then restore_cyril "netmon did not start within 30 seconds"; fail "Moving from Cyril to NetMon did not work. Cyril has been switched back on and is still monitoring this Mac."; fi
    sleep 5
    if ! pgrep -x "$PROC" >/dev/null 2>&1; then restore_cyril "netmon exited right after starting"; fail "Moving from Cyril to NetMon did not work. Cyril has been switched back on and is still monitoring this Mac."; fi
  fi
  printf 'moved from Cyril at %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$MARKER"
  ok "NetMon is running"
  load_updater
  disable_old_updater
  report_migration 1 "moved from Cyril: old agent stopped and disabled, $COPIED data item(s) copied, NetMon started"
elif [ -f "$MARKER" ]; then
  disable_old_updater   # finishes the job if an earlier run stopped half-way; does nothing once done
fi
if [ "$UNATTENDED" -eq 1 ]; then
  [ "$SILENT" -eq 0 ] || echo "NetMon installed. The agent is running; it appears on the dashboard's Install agents page within a minute or two. (The person at the Mac still has to allow Screen Recording once.)"
  exit 0
fi

# 6. Permissions: macOS asks the person, an installer cannot grant them -------------------------------------------------
echo
echo "  macOS needs the user's OK once. At first run the agent asks for:"
echo "    - Screen Recording (screenshots and window titles): System Settings > Privacy & Security > Screen Recording > netmon"
echo "    - Automation for each browser, to read the page address: click OK on the 'netmon wants to control ...' prompt"
if [ -z "$R" ] && [ "$CONSOLE_UID" -ne 0 ]; then
  launchctl asuser "$CONSOLE_UID" open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" 2>/dev/null || true
fi

# 7. Confirm the server really receives reports --------------------------------------------------------------------------
say "Waiting for Server Confirmation"
DEVICE="$(get_device)"
SEEN=""; EVENTS=0; WITH_APP=0
WAITS="${NETMON_TEST_WAITS:-${CYRIL_TEST_WAITS:-24}}"; STEP="${NETMON_TEST_STEP:-${CYRIL_TEST_STEP:-5}}"
i=0
while [ "$i" -lt "$WAITS" ]; do
  sleep "$STEP"; i=$((i + 1))
  RESP="$(curl -fsS -m 15 -H "X-Api-Key: $AGENT_KEY" "$SERVER_URL/api/agent/seen?device=$DEVICE&since=$SINCE" 2>/dev/null || true)"
  case "$RESP" in *'"seen":true'*) SEEN=1
      EVENTS="$(printf '%s' "$RESP" | sed -n 's/.*"events":\([0-9]*\).*/\1/p')"
      WITH_APP="$(printf '%s' "$RESP" | sed -n 's/.*"with_app":\([0-9]*\).*/\1/p')"; break ;; esac
done
diagnostics() {
  echo; echo "  Diagnostics:"
  if pgrep -x "$PROC" >/dev/null 2>&1; then echo "    netmon is running (pid $(pgrep -x "$PROC" | head -1))"; else echo "    netmon is NOT running"; fi
  if [ -f "$DATA/agent.log" ]; then echo "    last lines of $DATA/agent.log :"; tail -n 12 "$DATA/agent.log" | sed 's/^/      /'
  else echo "    no agent log yet at $DATA/agent.log (the agent never started)"; fi
  if [ -f "$DATA/crash.log" ]; then echo "    the agent crashed on startup:"; tail -n 15 "$DATA/crash.log" | sed 's/^/      /'; fi
  return 0
}
echo
if [ -n "$SEEN" ] && [ "${WITH_APP:-0}" -gt 0 ]; then
  printf '\033[32mInstallation Successful %sR/%sA\033[0m\n' "$EVENTS" "$WITH_APP"
elif [ -n "$SEEN" ]; then
  warn "The server is receiving reports, but none contain an app name. Screen Recording is probably not allowed yet (see above)."
  diagnostics
else
  warn "Installed, but the server has not received a report from this Mac yet. If a macOS permission prompt is waiting, click Allow."
  diagnostics
fi
echo
echo
