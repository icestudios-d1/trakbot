#!/bin/sh
# Renders the S3-identity config from env (SeaweedFS has no env-var
# interpolation in its own config file) and starts an all-in-one
# single-node SeaweedFS server with the S3 gateway enabled.
set -eu
: "${CYRIL_S3_ACCESS_KEY:?CYRIL_S3_ACCESS_KEY must be set}"
: "${CYRIL_S3_SECRET_KEY:?CYRIL_S3_SECRET_KEY must be set}"

sed -e "s/__ACCESS_KEY__/${CYRIL_S3_ACCESS_KEY}/" -e "s/__SECRET_KEY__/${CYRIL_S3_SECRET_KEY}/" \
  /etc/seaweedfs/s3config.json.template > /etc/seaweedfs/s3config.json

exec weed server -dir=/data -s3 -s3.port=8333 -s3.config=/etc/seaweedfs/s3config.json \
  -ip.bind=0.0.0.0 -master.volumeSizeLimitMB=1024 -volume.max=0
