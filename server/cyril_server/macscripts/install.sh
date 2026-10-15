#!/bin/bash
# Cyril agent installer for macOS. Needs only what macOS ships with (bash, curl, launchctl): no Homebrew, no Python.
# Safe to run again: it updates an existing install in place.
#
# The easiest way is the one-liner on the dashboard's "Install agents" page (the server fills in its own address
# and the agent key):
#
#   curl -fsSL 'https://cyril.example.com/install.sh?key=AGENT_KEY' | sudo bash
#
# Options (after "bash -s --"):  --user "Priya"   report under this name instead of the Mac login
#                                --unattended     no questions, no first-report wait (used by the daily updater)
#
# How it runs: the agent must live inside the logged-in user's session (that is where the windows and the screen
# are), so it is a LaunchAgent that starts at login and is restarted by launchd if it is closed. A second job, a
# LaunchDaemon running as root, re-runs this installer daily and at every start so new builds reach every Mac.
# Testing: set CYRIL_TEST_ROOT to prefix every path (the tests run this against a scratch directory).
set -euo pipefail

SERVER_URL='{{SERVER_URL}}'
AGENT_KEY='{{AGENT_KEY}}'
USER_NAME=""
UNATTENDED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="${2:-}"; shift 2 ;;
    --unattended) UNATTENDED=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

R="${CYRIL_TEST_ROOT:-}"
APPDIR="$R/Library/Application Support/Cyril"
BIN="$APPDIR/cyril"
CFG="$APPDIR/config.json"
DATA="$APPDIR/data"
AGENT_LABEL="in.icestudios.cyril"
UPDATER_LABEL="in.icestudios.cyril.updater"
AGENT_PLIST="$R/Library/LaunchAgents/$AGENT_LABEL.plist"
UPDATER_PLIST="$R/Library/LaunchDaemons/$UPDATER_LABEL.plist"

say()  { [ "$UNATTENDED" -eq 1 ] || printf '  \033[36m[..]\033[0m %s\n' "$1"; }
ok()   { [ "$UNATTENDED" -eq 1 ] || printf '  \033[32m[ok]\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m[!!]\033[0m %s\n' "$1"; }
fail() { printf '\n  \033[31m[xx]\033[0m %s\n\n' "$1" >&2; exit 1; }

[ "$UNATTENDED" -eq 1 ] || printf '\nCyril agent installer (macOS)\n'

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
TMP="$(mktemp "${TMPDIR:-/tmp}/cyril.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
if [ -n "$M_SHA" ] && [ "$CURRENT_SHA" = "$M_SHA" ]; then
  ok "Agent is already the published build ($INFO)"
  UP_TO_DATE=1
else
  UP_TO_DATE=0
  say "Downloading the agent"
  curl -fsSL -m 300 -o "$TMP" "$SERVER_URL$M_URL?h=${M_SHA:0:16}" || fail "Could not download $SERVER_URL$M_URL. The build may not be published."
  if [ -n "$M_SHA" ]; then
    [ "$(shasum -a 256 "$TMP" | cut -d' ' -f1)" = "$M_SHA" ] || fail "Downloaded file does not match the published checksum. Nothing was installed."
    ok "Checksum verified"
  fi
  chmod 755 "$TMP"
  if [ "$M_SELFTEST" = "1" ]; then
    "$TMP" --selftest || fail "The downloaded agent does not start (self-test failed). It is a broken build: rebuild with mac/build.sh and run this again. Nothing was installed."
    ok "Downloaded program starts correctly"
  fi
fi

# 4. Install -----------------------------------------------------------------------------------------------------------
if [ "$UP_TO_DATE" -eq 0 ]; then
  if [ -x "$BIN" ]; then
    if [ -z "$R" ]; then launchctl bootout "gui/$CONSOLE_UID/$AGENT_LABEL" 2>/dev/null || true; pkill -x cyril 2>/dev/null || true; sleep 1; fi
  fi
  install -m 755 "$TMP" "$BIN"
  ok "Agent installed to $APPDIR ($INFO)"
fi

# config: readable by every user (the agent runs as the logged-in user and must read it); only root can change it
OLD_USER=""
if [ -f "$CFG" ]; then OLD_USER="$(sed -n 's/.*"user" *: *"\([^"]*\)".*/\1/p' "$CFG" | head -1 || true)"; fi
[ -n "$USER_NAME" ] || USER_NAME="$OLD_USER"
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
{
  printf '{\n  "server": "%s",\n  "key": "%s"' "$(esc "$SERVER_URL")" "$(esc "$AGENT_KEY")"
  if [ -n "$USER_NAME" ]; then printf ',\n  "user": "%s"' "$(esc "$USER_NAME")"; fi
  printf '\n}\n'
} > "$CFG"
if [ -n "$NEW_BUILD_STAMP" ]; then printf '%s' "$NEW_BUILD_STAMP" > "$APPDIR/build.txt"; fi
mkdir -p "$DATA"
if [ -z "$R" ]; then
  chown -R root:wheel "$APPDIR"; chmod 755 "$APPDIR"; chmod 644 "$CFG" "$APPDIR/build.txt" 2>/dev/null || true
  chmod 1777 "$DATA"                       # the agent's log and queued data; written by whoever is logged in
fi
ok "Config written"

# updater script: re-runs this installer using the saved address and key
cat > "$APPDIR/update.sh" <<'UPD'
#!/bin/bash
# Run by the Cyril updater job (root). Does nothing unless a new build is published.
CFG="/Library/Application Support/Cyril/config.json"
[ -f "${CYRIL_TEST_ROOT:-}$CFG" ] && CFG="${CYRIL_TEST_ROOT:-}$CFG"
SERVER="$(sed -n 's/.*"server" *: *"\([^"]*\)".*/\1/p' "$CFG" | head -1)"
KEY="$(sed -n 's/.*"key" *: *"\([^"]*\)".*/\1/p' "$CFG" | head -1)"
[ -n "$SERVER" ] && [ -n "$KEY" ] || exit 1
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
    <string>/Library/Application Support/Cyril/cyril</string>
    <string>--config</string><string>/Library/Application Support/Cyril/config.json</string>
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
  <array><string>/bin/bash</string><string>/Library/Application Support/Cyril/update.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <key>ThrottleInterval</key><integer>600</integer>
  <key>StandardErrorPath</key><string>/Library/Application Support/Cyril/data/updater.log</string>
</dict>
</plist>
PLIST
if [ -z "$R" ]; then
  chown root:wheel "$AGENT_PLIST" "$UPDATER_PLIST"; chmod 644 "$AGENT_PLIST" "$UPDATER_PLIST"
  launchctl bootout system "$UPDATER_PLIST" 2>/dev/null || true
  launchctl bootstrap system "$UPDATER_PLIST" 2>/dev/null || true
fi
ok "Updater job installed (daily ~03:00 and at every start; picks up new builds)"

SINCE="$(date +%s)"; SINCE=$((SINCE - 5))
if [ -z "$R" ]; then
  if [ "$UP_TO_DATE" -eq 1 ] && pgrep -x cyril >/dev/null 2>&1; then
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
if [ "$UNATTENDED" -eq 1 ]; then exit 0; fi

# 6. Permissions: macOS asks the person, an installer cannot grant them -------------------------------------------------
echo
echo "  macOS needs the user's OK once. At first run the agent asks for:"
echo "    - Screen Recording (screenshots and window titles): System Settings > Privacy & Security > Screen Recording > cyril"
echo "    - Automation for each browser, to read the page address: click OK on the 'cyril wants to control ...' prompt"
if [ -z "$R" ] && [ "$CONSOLE_UID" -ne 0 ]; then
  launchctl asuser "$CONSOLE_UID" open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" 2>/dev/null || true
fi

# 7. Confirm the server really receives reports --------------------------------------------------------------------------
say "Waiting for the first report (up to ~2 minutes)"
DEVICE="$(ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null | awk -F'"' '/IOPlatformUUID/{print $4}' || true)"
[ -n "$DEVICE" ] || DEVICE="$(hostname)"
SEEN=""; EVENTS=0; WITH_APP=0
WAITS="${CYRIL_TEST_WAITS:-24}"; STEP="${CYRIL_TEST_STEP:-5}"
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
  if pgrep -x cyril >/dev/null 2>&1; then echo "    cyril is running (pid $(pgrep -x cyril | head -1))"; else echo "    cyril is NOT running"; fi
  if [ -f "$DATA/agent.log" ]; then echo "    last lines of $DATA/agent.log :"; tail -n 12 "$DATA/agent.log" | sed 's/^/      /'
  else echo "    no agent log yet at $DATA/agent.log (the agent never started)"; fi
  if [ -f "$DATA/crash.log" ]; then echo "    the agent crashed on startup:"; tail -n 15 "$DATA/crash.log" | sed 's/^/      /'; fi
  return 0
}
echo
if [ -n "$SEEN" ] && [ "${WITH_APP:-0}" -gt 0 ]; then
  printf '\033[32mCyril is installed and reporting: %s report(s) received, %s with app names.\033[0m\n' "$EVENTS" "$WITH_APP"
elif [ -n "$SEEN" ]; then
  warn "The server is receiving reports, but none contain an app name. Screen Recording is probably not allowed yet (see above)."
  diagnostics
else
  warn "Installed, but the server has not received a report from this Mac yet. If a macOS permission prompt is waiting, click Allow."
  diagnostics
fi
echo
echo "Dashboard: $SERVER_URL/install"
echo "To remove it:  curl -fsSL '$SERVER_URL/uninstall.sh' | sudo bash"
echo
