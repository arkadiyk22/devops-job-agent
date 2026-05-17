# Scheduled runs (macOS, ScoutSignal-style)

## Behavior

| When | What |
|------|------|
| **Every 30 minutes** (while Mac is awake) | `python3 run.py --fetch-only` — LinkedIn + Greenhouse, store in `jobs.db`, **no email** (reach-out scrape runs on digest send, not each poll) |
| **Once per day from 09:30** until 14:59 (Israel time) | **Morning digest** — email all stored jobs from your search (same listings repeat each digest) |
| **Once per day from 15:00** | **Afternoon digest** — same full list again (use `digest_ignore_*` in config to hide jobs later) |

If the laptop was asleep and both slots were missed, the morning digest can **catch up** when the Mac wakes after 15:00 (then afternoon runs in the same tick).

## One-time setup

```bash
cd /Users/arkadiykats/devops-job-agent
chmod +x extras/*.sh
pip install -r requirements.txt
playwright install chromium
python3 run.py --linkedin-login
```

### Remove column in digest email (hide jobs)

Each digest row has **Remove: Yes** — click to hide that job from future digests and fetches.

List hidden jobs and restore:

```bash
python3 run.py --send-removed-email
```

The link opens `http://127.0.0.1:8791/remove?...` on your Mac — keep the remove server running:

```bash
# foreground (testing)
python3 run.py --digest-remove-server

# or install LaunchAgent (recommended)
cp extras/com.job-agent.remove-server.example.plist ~/Library/LaunchAgents/com.job-agent.remove-server.plist
launchctl load ~/Library/LaunchAgents/com.job-agent.remove-server.plist
```

Hidden URLs are stored in `~/.job-agent/digest_ignore_links.json` (merged with `digest_ignore_links` in config).

Put `EMAIL_USER`, `EMAIL_PASS`, `EMAIL_TO` in `.env` in the repo (or `~/.job-agent/.env`).

## Install LaunchAgent (every 30 min)

1. Edit paths in `extras/com.job-agent.two-slot-interval.example.plist` if needed.
2. Install:

```bash
cp extras/com.job-agent.two-slot-interval.example.plist ~/Library/LaunchAgents/com.job-agent.two-slot-interval.plist
launchctl load ~/Library/LaunchAgents/com.job-agent.two-slot-interval.plist
```

Logs: `/tmp/job-agent-two-slot.out.log`, `/tmp/job-agent-two-slot.err.log`, and `~/.job-agent/logs/two-slot-decisions.log`.

Unload:

```bash
launchctl unload ~/Library/LaunchAgents/com.job-agent.two-slot-interval.plist
```

## Manual commands

```bash
# Poll only (no email)
./extras/job-agent-run.sh   # with JOB_AGENT_EXTRA_ARGS=--fetch-only

# Morning-style email (pending jobs)
JOB_AGENT_EXTRA_ARGS="--send-pending-email --digest-slot morning" ./extras/job-agent-run.sh

# Full manual run: fetch + email everything pending
python3 run.py
```

## Test the schedule logic (no fetch)

```bash
python3 extras/daily_two_slot.py --dry-run -- \
  /bin/bash extras/job-agent-email-slot.sh
```
