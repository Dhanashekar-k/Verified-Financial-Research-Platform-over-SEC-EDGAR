#!/bin/bash
# PostToolUse hook for Edit|Write: format and lint-fix edited Python files.
# Never fails the edit; lint problems still show up in `make lint` and CI.
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
case "$FILE_PATH" in
  *.py)
    cd "$CLAUDE_PROJECT_DIR" || exit 0
    uv run ruff format "$FILE_PATH" >/dev/null 2>&1
    uv run ruff check --fix "$FILE_PATH" >/dev/null 2>&1
    ;;
esac
exit 0
