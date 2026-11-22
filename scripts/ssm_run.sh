#!/usr/bin/env bash
# Run a shell script on the serving box through SSM and print its output.
#
#   scripts/ssm_run.sh <instance-id> '<bash script text>' [timeout-seconds]
#
# Long work (loads, index builds, the bench) must run under nohup ON the box and be polled with
# short calls, so an expired laptop session cannot interrupt it. Output is capped by SSM (24 KB).
set -uo pipefail
iid="$1"; script="$2"; timeout="${3:-600}"
export AWS_PROFILE="${AWS_PROFILE:-edgarqa}" AWS_REGION="${AWS_REGION:-us-east-2}"
params="$(python3 -c 'import json,sys; print(json.dumps({"commands":[sys.argv[1]]}))' "$script")"
cid="$(aws ssm send-command --instance-ids "$iid" --document-name AWS-RunShellScript \
  --timeout-seconds "$timeout" --parameters "$params" --query Command.CommandId --output text)" || exit 3
for _ in $(seq 1 $((timeout / 3 + 20))); do
  if ! status="$(aws ssm get-command-invocation --command-id "$cid" --instance-id "$iid" \
      --query Status --output text 2>/tmp/ssm_run.err)"; then
    # the invocation is not registered for a moment after send-command; anything else is real
    grep -q InvocationDoesNotExist /tmp/ssm_run.err || { cat /tmp/ssm_run.err >&2; exit 4; }
    status=Pending
  fi
  case "$status" in Pending|InProgress|Delayed) sleep 3 ;; *) break ;; esac
done
aws ssm get-command-invocation --command-id "$cid" --instance-id "$iid" \
  --query '[StandardOutputContent, StandardErrorContent]' --output text | sed 's/\t/\n--- stderr ---\n/'
echo "[ssm status: $status]"
[ "$status" = Success ]
