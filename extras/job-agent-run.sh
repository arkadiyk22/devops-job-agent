#!/usr/bin/env bash
# Unattended job-agent run for launchd/cron.
# Secrets: EMAIL_USER, EMAIL_PASS, EMAIL_TO in repo .env or ~/.job-agent/.env
#
# Overrides:
#   JOB_AGENT_ROOT          (default: directory containing this extras/ folder)
#   JOB_AGENT_CONFIG        (default: $JOB_AGENT_ROOT/config.json)
#   JOB_AGENT_PYTHON        (default: $JOB_AGENT_ROOT/.venv/bin/python3)
#   JOB_AGENT_EXTRA_ARGS    (e.g. --fetch-only or --send-pending-email --digest-slot morning)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${JOB_AGENT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PY="${JOB_AGENT_PYTHON:-$ROOT/.venv/bin/python3}"
CONFIG="${JOB_AGENT_CONFIG:-$ROOT/config.json}"

if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

declare -a EXTRA=()
if [[ -n "${JOB_AGENT_EXTRA_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  EXTRA=(${JOB_AGENT_EXTRA_ARGS})
fi

cd "$ROOT"

# launchd does not load .env; python-dotenv also runs, but export for any shell children
for envfile in "$ROOT/.env" "$HOME/.job-agent/.env"; do
  if [[ -f "$envfile" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$envfile"
    set +a
    break
  fi
done

if ((${#EXTRA[@]} > 0)); then
  exec "$PY" run.py --config "$CONFIG" "${EXTRA[@]}"
else
  exec "$PY" run.py --config "$CONFIG"
fi
