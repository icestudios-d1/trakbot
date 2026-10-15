#!/bin/sh
# Renders the S3-identity config from env (SeaweedFS has no env-var
# interpolation in its own config file) and starts an all-in-one
# single-node SeaweedFS server with the S3 gateway enabled.
#
# The secrets are only ever piped through sed as *data* (stdin), never
# embedded into a sed *script* as replacement text -- an earlier version did
# the latter via a template + `s/__PLACEHOLDER__/$SECRET/`, which broke
# ("bad option in substitution expression") the moment a secret containing
# `/` was used, since that's sed's own delimiter. A secret containing the
# JSON characters `"` or `\` is still handled safely below.
set -eu
: "${CYRIL_S3_ACCESS_KEY:?CYRIL_S3_ACCESS_KEY must be set}"
: "${CYRIL_S3_SECRET_KEY:?CYRIL_S3_SECRET_KEY must be set}"

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

ACCESS_KEY=$(json_escape "$CYRIL_S3_ACCESS_KEY")
SECRET_KEY=$(json_escape "$CYRIL_S3_SECRET_KEY")

cat > /etc/seaweedfs/s3config.json <<EOF
{
  "identities": [
    {
      "name": "cyril",
      "credentials": [{ "accessKey": "$ACCESS_KEY", "secretKey": "$SECRET_KEY" }],
      "actions": ["Admin", "Read", "Write"]
    }
  ]
}
EOF

# -volume.max=0 means "work it out from free disk space / volume size", which comes out as ZERO volumes (every
# write then fails with "no free volumes") on a small disk. An explicit ceiling lets volumes grow as screenshots arrive.
exec weed server -dir=/data -s3 -s3.port=8333 -s3.config=/etc/seaweedfs/s3config.json \
  -ip.bind=0.0.0.0 -master.volumeSizeLimitMB=1024 -volume.max=1000
