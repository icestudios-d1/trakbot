#!/bin/bash
# Makes the ONE certificate every Mac agent build is signed with. Run it once, on any Mac (or Linux), then store the
# two values it prints as GitHub secrets. It is self-signed: it does not make macOS "trust" the agent, it only gives
# every build the same identity, so the Screen Recording permission a person allowed keeps applying after updates.
#
#   ./mac/make-signing-cert.sh              # writes into ~/cyril-signing
#
# KEEP THE FOLDER SAFE AND PRIVATE. Anyone holding it can sign a program macOS will treat as the agent. If it is
# lost, make a new one: every Mac then has to allow Screen Recording once more.
set -euo pipefail
OUT="${1:-$HOME/cyril-signing}"
NAME="Cyril Agent Signing"
[ ! -e "$OUT/cyril-signing.p12" ] || { echo "$OUT/cyril-signing.p12 already exists. Reuse it; a new certificate makes every Mac ask again." >&2; exit 1; }
mkdir -p "$OUT"; chmod 700 "$OUT"; cd "$OUT"
umask 077

cat > openssl.cnf <<CNF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
CNF

PASS="$(openssl rand -hex 16)"
openssl req -x509 -newkey rsa:2048 -nodes -days 7300 -config openssl.cnf -keyout key.pem -out cert.pem 2>/dev/null
# macOS can only import the older .p12 format; OpenSSL 3 needs to be told to write it (LibreSSL on a Mac already does)
LEGACY=""; case "$(openssl version)" in OpenSSL\ 3*) LEGACY="-legacy" ;; esac
openssl pkcs12 -export $LEGACY -inkey key.pem -in cert.pem -name "$NAME" -out cyril-signing.p12 -passout "pass:$PASS"
openssl base64 -A -in cyril-signing.p12 -out MAC_SIGN_P12.txt
printf '%s' "$PASS" > MAC_SIGN_P12_PASSWORD.txt
rm -f key.pem openssl.cnf

echo "Certificate made (valid 20 years) in $OUT"
echo
echo "On GitHub: the repository, Settings, Secrets and variables, Actions, New repository secret. Add two:"
echo "  MAC_SIGN_P12            the whole content of $OUT/MAC_SIGN_P12.txt"
echo "  MAC_SIGN_P12_PASSWORD   the content of $OUT/MAC_SIGN_P12_PASSWORD.txt"
echo "On a Mac, 'pbcopy < $OUT/MAC_SIGN_P12.txt' copies the first one to the clipboard."
echo
echo "Then run the 'agents' workflow (Actions, agents, Run workflow). Its Mac jobs print 'Signed.' when it worked."
