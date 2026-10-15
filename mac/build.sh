#!/bin/bash
# Builds the macOS agent and publishes it to the server's dist folder. Run this ON a Mac, in the repo:
#
#   ./mac/build.sh            # version 0.1.0
#   ./mac/build.sh 0.2.0
#
# PyInstaller builds for the CPU it runs on, so run it once on an Apple Silicon Mac (arm64) and once on an Intel Mac
# (x86_64) to support both; each run writes its own files and neither touches the other's. Needs Python 3.11+
# (python.org, or `xcode-select --install`). The output goes straight into server/dist, which the server serves
# (docker-compose bind-mounts it): no copy and no restart. If you build on a Mac other than the server, copy the two
# files it prints into the server's server/dist folder.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${OUT_DIR:-$ROOT/server/dist}"
VERSION="${1:-0.1.0}"
ARCH="$(uname -m)"; [ "$ARCH" = "arm64" ] || ARCH="x86_64"
mkdir -p "$OUT"

VENV="$ROOT/build/venv-mac-$ARCH"
python3 -m venv "$VENV"
# shellcheck disable=SC1091
. "$VENV/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet pyinstaller mss pillow
pip install --quiet -r "$ROOT/agent/requirements.txt"      # pulls pyobjc (Cocoa, Quartz) on macOS

BIN="$OUT/cyril-mac-$ARCH"
pyinstaller --onefile --name "cyril-mac-$ARCH" \
  --paths "$ROOT/agent" \
  --hidden-import AppKit --hidden-import Quartz --hidden-import Foundation \
  --distpath "$OUT" --workpath "$ROOT/build/mac-$ARCH" --specpath "$ROOT/build/mac-$ARCH" \
  "$ROOT/agent/cyril_entry.py"

# Never publish a build that cannot start: --selftest exits 0 once the program (and its Cocoa/Quartz pieces) loaded.
if ! "$BIN" --selftest; then
  rm -f "$BIN" "$OUT/manifest-mac-$ARCH.json"
  echo "The built agent failed its self-test (exit code $?). Nothing was published." >&2
  exit 1
fi
echo "Self-test passed."

SHA="$(shasum -a 256 "$BIN" | cut -d' ' -f1)"
BUILD="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || true)"
printf '{"version":"%s","build":"%s","arch":"%s","url":"/dist/cyril-mac-%s","sha256":"%s","selftest":true}\n' \
  "$VERSION" "$BUILD" "$ARCH" "$ARCH" "$SHA" > "$OUT/manifest-mac-$ARCH.json"

echo
echo "Built (version $VERSION, build ${BUILD:-unknown}, $ARCH):"
echo "  $BIN ($(du -h "$BIN" | cut -f1)), sha256 $SHA"
echo "  $OUT/manifest-mac-$ARCH.json"
echo
echo "Published: these are in server/dist, which the server serves directly (no copy, no restart)."
echo "New Macs: use the command on the dashboard's 'Install agents' page (Mac tab)."
echo "Existing Macs: their updater job re-runs the installer daily and at every start; to do it now run:"
echo "  sudo launchctl kickstart -k system/in.icestudios.cyril.updater"
