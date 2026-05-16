#!/usr/bin/env bash
# ScoutSignal-style schedule for devops-job-agent:
#   - Every launchd tick (~30 min): fetch jobs into jobs.db (no email)
#   - Once per day 09:30–14:59 Israel: morning digest email
#   - Once per day from 15:00: afternoon digest email
#
# Install: see extras/README.md

set -euo pipefail

export TZ="${TZ:-Asia/Jerusalem}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="${JOB_AGENT_STATE_DIR:-$HOME/.job-agent}"
LOG_DIR="${JOB_AGENT_LOG_DIR:-$STATE_DIR/logs}"
mkdir -p "$LOG_DIR" "$STATE_DIR"

export JOB_AGENT_ROOT="$ROOT"
export JOB_AGENT_CONFIG="${JOB_AGENT_CONFIG:-$ROOT/config.json}"

# 1) Poll sources — store payloads, do not email
export JOB_AGENT_EXTRA_ARGS="--fetch-only"
/bin/bash "$SCRIPT_DIR/job-agent-run.sh"

# 2) Morning / afternoon email slots (at most once each per calendar day)
unset JOB_AGENT_EXTRA_ARGS
exec /usr/bin/python3 "$SCRIPT_DIR/daily_two_slot.py" \
  --state-file "$STATE_DIR/.daily-two-slot-state.json" \
  --morning-start-hour 9 \
  --morning-start-minute 30 \
  --afternoon-start-hour 15 \
  --afternoon-start-minute 0 \
  --log-file "$LOG_DIR/two-slot-decisions.log" \
  -- \
  /bin/bash "$SCRIPT_DIR/job-agent-email-slot.sh"
