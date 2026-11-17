#!/bin/bash
# PreToolUse hook for Edit|Write.
# Blocks Claude Code from editing:
#   1. secrets and Terraform state (always)
#   2. gold evaluation data (always; drafts under eval/sets/drafts/ are allowed)
#   3. Shekar-owned core modules, once they exist (Claude may create the initial stub).
#      Launch with EDGARQA_ALLOW_CORE_EDITS=1 to let Claude apply a diff you already reviewed.
# Exit code 2 blocks the tool call and sends the stderr message back to Claude.
# Note: this covers the Edit/Write tools only. Shell edits are covered by CLAUDE.md rule 2
# and by `make check-gold` (checksums verified in CI).

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$FILE_PATH" ] && exit 0
FILE_PATH="${FILE_PATH//\\//}"
REL="${FILE_PATH#"$CLAUDE_PROJECT_DIR"/}"

block() {
  echo "Blocked by .claude/hooks/protect-paths.sh: $REL is $1. See CLAUDE.md hard rules." >&2
  exit 2
}

# 1 + 2: always protected
case "$REL" in
  .env|.env.local|*/.env) block "a secrets file" ;;
  *.tfstate|*.tfstate.backup|*/.terraform/*) block "Terraform state" ;;
  eval/sets/drafts/*) ;;  # drafts are allowed
  eval/sets/*.jsonl|eval/sets/CHECKSUMS|eval/judges/calibration.jsonl) \
    block "gold evaluation data curated by hand" ;;
esac

# 3: Shekar-owned modules (allowed only if the file doesn't exist yet, or with the override)
OWNED=(
  "pipelines/common/fiscal_ref.py"
  "app/engine/plan/compiler.py"
  "app/engine/plan/periods.py"
  "app/engine/plan/metric_catalog.yaml"
  "app/engine/verify/numeric.py"
  "app/engine/verify/quotes.py"
  "app/engine/verify/policy.py"
  "app/engine/sql_guard/guard.py"
  "app/engine/router/cascade.py"
  "ml/router/model_fns.py"
  "eval/metrics.py"
)

for owned in "${OWNED[@]}"; do
  if [[ "$REL" == "$owned" ]]; then
    if [[ "${EDGARQA_ALLOW_CORE_EDITS:-0}" == "1" ]]; then
      exit 0
    fi
    if [[ -e "$FILE_PATH" ]]; then
      block "a Shekar-owned core module (write tests, review, or propose a diff in chat instead)"
    fi
    exit 0  # creating the initial stub is allowed
  fi
done

exit 0
