#!/usr/bin/env bash
# Called by daily_two_slot.py when a morning or afternoon email window is active.
# JOB_AGENT_SLOT is set to "morning" or "afternoon".

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SLOT="${JOB_AGENT_SLOT:-}"

case "$SLOT" in
  morning)
    export JOB_AGENT_EXTRA_ARGS="--send-pending-email --digest-slot morning"
    ;;
  afternoon)
    export JOB_AGENT_EXTRA_ARGS="--send-pending-email --digest-slot afternoon"
    ;;
  digest)
    export JOB_AGENT_EXTRA_ARGS="--send-pending-email --digest-slot digest"
    ;;
  *)
    echo "job-agent-email-slot.sh: unknown JOB_AGENT_SLOT=$SLOT" >&2
    exit 2
    ;;
esac

exec /bin/bash "$SCRIPT_DIR/job-agent-run.sh"
