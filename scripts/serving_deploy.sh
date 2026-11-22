#!/usr/bin/env bash
# Publish the serving bundle (loader, bench, retrieval, the shared embedding/model code, SQL templates)
# to s3://<lake>/artifacts/serving/ for the serving box to pull.
#
#   scripts/serving_deploy.sh          dry run: build the bundle, print what would be uploaded
#   YES=1 scripts/serving_deploy.sh    upload (a cloud write: you run it, or ask Claude to)
set -euo pipefail
cd "$(dirname "$0")/.."
PROFILE="${AWS_PROFILE:-edgarqa}"; REGION="${AWS_REGION:-us-east-2}"
aws_() { aws --profile "$PROFILE" --region "$REGION" "$@"; }
account="$(aws_ sts get-caller-identity --query Account --output text)"
bucket="${LAKE_BUCKET:-edgarqa-lake-${account}}"
sha="$(git rev-parse --short=12 HEAD)"; dirty=""
[ -n "$(git status --porcelain --untracked-files=no)" ] && dirty="-dirty"
if [ -n "$dirty" ] && [ "${YES:-}" = "1" ] && [ "${ALLOW_DIRTY:-}" != "1" ]; then
  echo "refusing to publish a dirty tree (a bench would not match any commit); commit first or ALLOW_DIRTY=1" >&2; exit 1
fi
mkdir -p dist
bundle="dist/serving-${sha}.tgz"
tar --exclude='__pycache__' --exclude='*.pyc' -czf "$bundle" \
  serving app/__init__.py app/engine/__init__.py app/engine/retrieval \
  pipelines/__init__.py pipelines/common
echo "bundle $bundle ($(du -h "$bundle" | cut -f1)), version ${sha}${dirty}"
if [ "${YES:-}" != "1" ]; then
  echo "dry run; add YES=1 to upload to s3://${bucket}/artifacts/serving/serving.tgz"; exit 0
fi
aws_ s3 cp "$bundle" "s3://${bucket}/artifacts/serving/serving.tgz" --only-show-errors
printf '%s\n' "${sha}${dirty}" | aws_ s3 cp - "s3://${bucket}/artifacts/serving/VERSION" --only-show-errors
echo "uploaded s3://${bucket}/artifacts/serving/serving.tgz (${sha}${dirty})"
