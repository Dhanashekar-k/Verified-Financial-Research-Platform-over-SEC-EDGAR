#!/usr/bin/env bash
# Apply branch protection to main, simulating a team merge gate.
# Requires: gh auth login (as the repo owner). Run once, after the first push.
set -euo pipefail
REPO="Dhanashekar-k/Verified-Financial-Research-Platform-over-SEC-EDGAR"
BRANCH="main"

gh api -X PUT "repos/$REPO/branches/$BRANCH/protection" --input - <<'JSON'
{
  "required_status_checks": { "strict": true, "contexts": ["lint-and-test"] },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "require_code_owner_reviews": true,
    "dismiss_stale_reviews": true
  },
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "restrictions": null
}
JSON
echo "Protection applied to $BRANCH. (enforce_admins=false lets you admin-merge your own self-reviewed PRs.)"
