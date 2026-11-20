#!/usr/bin/env bash
# Publish the Glue job code: the two entry scripts to s3://<lake>/scripts/ and the code bundle
# (pipelines/**/*.py) to s3://<lake>/artifacts/glue/pipelines.zip, which every job lists in
# --extra-py-files. A VERSION object records the git sha the bundle was built from.
#
#   scripts/glue_deploy.sh          dry run: build the bundle, print what would be uploaded
#   YES=1 scripts/glue_deploy.sh    upload (a cloud write: you run it, or ask Claude to)
set -euo pipefail

cd "$(dirname "$0")/.."
PROFILE="${AWS_PROFILE:-edgarqa}"
REGION="${AWS_REGION:-us-east-2}"
aws_() { aws --profile "$PROFILE" --region "$REGION" "$@"; }

account="$(aws_ sts get-caller-identity --query Account --output text)"
bucket="${LAKE_BUCKET:-edgarqa-lake-${account}}"
sha="$(git rev-parse --short=12 HEAD)"
dirty=""
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then dirty="-dirty"; fi

uv run python -m pipelines.cloud.package --out dist
zip="dist/pipelines-${sha}.zip"
version="${sha}${dirty}"

uploads=(
  "pipelines/glue/fsds_to_iceberg_job.py|s3://${bucket}/scripts/fsds_to_iceberg_job.py"
  "pipelines/glue/normalize_facts_job.py|s3://${bucket}/scripts/normalize_facts_job.py"
  "${zip}|s3://${bucket}/artifacts/glue/pipelines.zip"
)

if [ "${YES:-}" != "1" ]; then
  echo "dry run (version ${version}); add YES=1 to upload:"
  for u in "${uploads[@]}"; do echo "  ${u%%|*} -> ${u##*|}"; done
  exit 0
fi

for u in "${uploads[@]}"; do
  aws_ s3 cp "${u%%|*}" "${u##*|}" --only-show-errors
  echo "uploaded ${u##*|}"
done
printf '%s\n' "$version" | aws_ s3 cp - "s3://${bucket}/artifacts/glue/VERSION" --only-show-errors
echo "uploaded s3://${bucket}/artifacts/glue/VERSION (${version})"
