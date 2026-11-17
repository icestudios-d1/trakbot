#!/bin/bash
# ONE-TIME: makes the self-signed certificate every Mac build of the agent is signed with.
#
# Why: macOS remembers Screen Recording per program. For a program without a real signature (what PyInstaller makes
# by default) "the program" means that exact file, so every new build lost the permission on every Mac. Signed with
# the same certificate each time, macOS recognises every later build as the same program and the permission stays.
#
#   ./mac/make-signing-cert.sh          # any Mac or Linux machine with openssl
#
# It writes netmon-signing.p12 + its password into ./netmon-signing/ and prints the two GitHub secrets to add
# (repo Settings > Secrets and variables > Actions):  MAC_SIGN_P12_B64  and  MAC_SIGN_P12_PASSWORD.
#
# KEEP THE .p12 AND ITS PASSWORD SAFE AND NEVER MAKE A NEW ONE: a different certificate is a different program to
# macOS, and every Mac would have to allow Screen Recording again. Do not commit the folder.
set -euo pipefail
OUT="${1:-netmon-signing}"
NAME="NetMon Code Signing (ICE Studios)"
[ ! -e "$OUT/netmon-signing.p12" ] || { echo "$OUT/netmon-signing.p12 already exists: keep using it (see the header)." >&2; exit 1; }
mkdir -p "$OUT"; chmod 700 "$OUT"
umask 077

PASS="$(openssl rand -hex 24)"
cat > "$OUT/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
O = ICE Studios
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
subjectKeyIdentifier = hash
EOF
# 20 years: the builds are not timestamped, so the certificate must outlive every build still installed
openssl req -x509 -newkey rsa:2048 -nodes -days 7300 -config "$OUT/cert.cnf" \
  -keyout "$OUT/key.pem" -out "$OUT/cert.pem" 2>/dev/null
# 3DES/SHA-1 PKCS#12: the only kind macOS's `security import` reads on every macOS version (OpenSSL 3's default is not)
openssl pkcs12 -export -inkey "$OUT/key.pem" -in "$OUT/cert.pem" -name "$NAME" \
  -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES -macalg sha1 \
  -out "$OUT/netmon-signing.p12" -passout "pass:$PASS"
printf '%s\n' "$PASS" > "$OUT/password.txt"
rm -f "$OUT/key.pem" "$OUT/cert.cnf"   # the key lives only inside the .p12 now

FP="$(openssl x509 -in "$OUT/cert.pem" -noout -fingerprint -sha1 | sed 's/.*=//; s/://g')"
base64 < "$OUT/netmon-signing.p12" | tr -d '\n' > "$OUT/p12.base64.txt"
cat <<EOF

Made: $OUT/netmon-signing.p12  (certificate "$NAME", SHA-1 $FP)

Add two repository secrets (GitHub > Settings > Secrets and variables > Actions > New repository secret):
  MAC_SIGN_P12_B64       = the contents of $OUT/p12.base64.txt   (one long line)
  MAC_SIGN_P12_PASSWORD  = the contents of $OUT/password.txt

Then store the whole $OUT folder somewhere safe (a password manager) and delete it from this machine.
EOF
