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

# Signing. macOS remembers Screen Recording per program, and for an unsigned (ad-hoc signed) build that means that
# exact file: every new build would lose the permission on every Mac. Signed with the same certificate each time,
# all builds are the same program to macOS. The certificate is made once with mac/make-signing-cert.sh and given
# as MAC_SIGN_P12_B64 + MAC_SIGN_P12_PASSWORD (GitHub secrets in CI). A CI build without it is refused, never
# published; a build by hand without it only warns (fine for trying things, not for Macs in use).
SIGN_ARGS=()
SIGN_ID=""
if [ -n "${MAC_SIGN_P12_B64:-}" ]; then
  [ -n "${MAC_SIGN_P12_PASSWORD:-}" ] || { echo "MAC_SIGN_P12_B64 is set but MAC_SIGN_P12_PASSWORD is not." >&2; exit 1; }
  SIGNDIR="$ROOT/build/signing-$ARCH"
  rm -rf "$SIGNDIR"; mkdir -p "$SIGNDIR"; chmod 700 "$SIGNDIR"
  KC="$SIGNDIR/netmon-signing.keychain-db"
  KCPW="$(openssl rand -hex 16)"
  printf '%s' "$MAC_SIGN_P12_B64" | base64 --decode > "$SIGNDIR/s.p12"
  openssl pkcs12 -in "$SIGNDIR/s.p12" -nokeys -clcerts -passin env:MAC_SIGN_P12_PASSWORD 2>/dev/null \
    | openssl x509 -out "$SIGNDIR/cert.pem"
  SIGN_ID="$(openssl x509 -in "$SIGNDIR/cert.pem" -noout -fingerprint -sha1 | sed 's/.*=//; s/://g')"
  # a throwaway keychain just for this build, added to the search list so codesign finds the key
  security create-keychain -p "$KCPW" "$KC"
  security set-keychain-settings -lut 3600 "$KC"
  security unlock-keychain -p "$KCPW" "$KC"
  security import "$SIGNDIR/s.p12" -k "$KC" -P "$MAC_SIGN_P12_PASSWORD" -T /usr/bin/codesign >/dev/null
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KCPW" "$KC" >/dev/null
  # shellcheck disable=SC2046
  security list-keychains -d user -s "$KC" $(security list-keychains -d user | tr -d '"')
  # codesign signs with an untrusted self-signed certificate too; trusting it only quiets warnings (CI has sudo)
  if [ -n "${CI:-}" ]; then
    sudo security add-trusted-cert -d -r trustRoot -p codeSign -k "$KC" "$SIGNDIR/cert.pem" 2>/dev/null || true
  fi
  rm -f "$SIGNDIR/s.p12"
  SIGN_ARGS=(--codesign-identity "$SIGN_ID")
  echo "Signing with certificate $SIGN_ID"
elif [ -n "${CI:-}" ]; then
  echo "No signing certificate (MAC_SIGN_P12_B64 / MAC_SIGN_P12_PASSWORD secrets). An unsigned build would make" >&2
  echo "every Mac lose Screen Recording, so nothing was published. Make one with mac/make-signing-cert.sh." >&2
  exit 1
else
  echo "WARNING: no MAC_SIGN_P12_B64 given, building UNSIGNED: Macs would lose Screen Recording with this build." >&2
fi

BIN="$OUT/netmon-mac-$ARCH"
pyinstaller --onefile --name "netmon-mac-$ARCH" ${SIGN_ARGS[@]+"${SIGN_ARGS[@]}"} \
  --paths "$ROOT/agent" \
  --collect-data certifi --collect-all cryptography --hidden-import truststore --hidden-import AppKit --hidden-import Quartz --hidden-import Foundation \
  --distpath "$OUT" --workpath "$ROOT/build/mac-$ARCH" --specpath "$ROOT/build/mac-$ARCH" \
  "$ROOT/agent/cyril_entry.py"

# Never publish a build that cannot start: --selftest exits 0 once the program (and its Cocoa/Quartz pieces) loaded.
if ! "$BIN" --selftest; then
  rm -f "$BIN" "$OUT/manifest-mac-$ARCH.json"
  echo "The built agent failed its self-test (exit code $?). Nothing was published." >&2
  exit 1
fi
echo "Self-test passed."

if [ -n "$SIGN_ID" ]; then
  # Never publish a build that is not signed by OUR certificate: the leaf hash in the designated requirement is
  # what macOS matches the Screen Recording permission against.
  codesign --verify --strict "$BIN"
  REQ="$(codesign -d -r- "$BIN" 2>&1)"
  if ! printf '%s' "$REQ" | grep -qi "certificate leaf = H\"$SIGN_ID\""; then
    rm -f "$BIN" "$OUT/manifest-mac-$ARCH.json"
    printf 'The build is not signed with certificate %s. Nothing was published.\n%s\n' "$SIGN_ID" "$REQ" >&2
    exit 1
  fi
  echo "Signed: $(printf '%s' "$REQ" | grep designated)"
  security delete-keychain "$KC" 2>/dev/null || true
fi

SHA="$(shasum -a 256 "$BIN" | cut -d' ' -f1)"
BUILD="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || true)"
printf '{"version":"%s","build":"%s","arch":"%s","url":"/dist/netmon-mac-%s","sha256":"%s","selftest":true}\n' \
  "$VERSION" "$BUILD" "$ARCH" "$ARCH" "$SHA" > "$OUT/manifest-mac-$ARCH.json"

echo
echo "Built (version $VERSION, build ${BUILD:-unknown}, $ARCH):"
echo "  $BIN ($(du -h "$BIN" | cut -f1)), sha256 $SHA"
echo "  $OUT/manifest-mac-$ARCH.json"
echo
echo "Published: these are in server/dist, which the server serves directly (no copy, no restart)."
echo "New Macs: use the command on the dashboard's 'Install agents' page (Mac tab)."
echo "Existing Macs: their updater job re-runs the installer daily and at every start; to do it now run:"
echo "  sudo launchctl kickstart -k system/in.icestudios.netmon.updater"
