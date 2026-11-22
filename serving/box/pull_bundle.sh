#!/usr/bin/env bash
# Replace /opt/edgarqa/code with the bundle published by scripts/serving_deploy.sh (on the box, as root).
set -euo pipefail
BUCKET="${1:?usage: pull_bundle.sh <lake-bucket>}"
tmp="$(mktemp -d)"
aws s3 cp "s3://${BUCKET}/artifacts/serving/serving.tgz" "$tmp/serving.tgz" --only-show-errors
aws s3 cp "s3://${BUCKET}/artifacts/serving/VERSION" "$tmp/VERSION" --only-show-errors
rm -rf /opt/edgarqa/code && mkdir -p /opt/edgarqa/code
tar -xzf "$tmp/serving.tgz" -C /opt/edgarqa/code
cp "$tmp/VERSION" /opt/edgarqa/code/VERSION
echo "bundle $(cat /opt/edgarqa/code/VERSION) in /opt/edgarqa/code"
