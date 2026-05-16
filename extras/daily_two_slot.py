#!/usr/bin/env python3
"""
Run a command at most once per calendar day in each of two wall-clock windows (local TZ, e.g. Asia/Jerusalem):

  - morning: from morning_start (default 09:00) through one minute before afternoon_start
  - afternoon: from afternoon_start (default 15:00) through end of day

Before morning_start, neither slot runs. If the machine was asleep and both slots are still
pending once afternoon_start has passed, optional catch-up runs the morning slot first,
then the afternoon slot, in one invocation.

State is a small JSON file updated only after the wrapped command exits 0.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


def _local_ymd_and_minutes() -> Tuple[str, int]:
    """Return (YYYY-MM-DD, minutes_since_midnight) using TZ from the environment."""
    lt = time.localtime()
    ymd = time.strftime("%Y-%m-%d", lt)
    minutes = lt.tm_hour * 60 + lt.tm_min
    return ymd, minutes


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".daily-two-slot-", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return raw
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def _log_line(path: Optional[str], msg: str) -> None:
    if not path:
        return
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime()) + " " + msg + "\n")
    except OSError:
        pass


@dataclass(frozen=True)
class Plan:
    run_morning: bool
    run_afternoon: bool


def _plan(
    minutes: int,
    morning_start_minutes: int,
    afternoon_start_minutes: int,
    morning_done_today: bool,
    afternoon_done_today: bool,
    catch_up_morning: bool,
) -> Plan:
    in_morning_window = morning_start_minutes <= minutes < afternoon_start_minutes
    in_afternoon_window = minutes >= afternoon_start_minutes

    run_morning = False
    if not morning_done_today:
        if in_morning_window:
            run_morning = True
        elif catch_up_morning and in_afternoon_window:
            run_morning = True

    run_afternoon = bool(not afternoon_done_today and in_afternoon_window)
    return Plan(run_morning=run_morning, run_afternoon=run_afternoon)


def main(argv: List[str]) -> int:
    if "--" not in argv:
        print("usage: daily_two_slot.py [options] -- <command> [args...]", file=sys.stderr)
        return 2
    sep = argv.index("--")
    pre_argv = argv[:sep]
    cmd = list(argv[sep + 1 :])
    if not cmd:
        print("usage: daily_two_slot.py [options] -- <command> [args...]", file=sys.stderr)
        return 2

    p = argparse.ArgumentParser(description="Two-slot daily command runner (TZ via $TZ).")
    p.add_argument(
        "--state-file",
        default=os.path.join(os.path.expanduser("~"), ".job-agent", ".daily-two-slot-state.json"),
        help="JSON state path (default: ~/.job-agent/.daily-two-slot-state.json)",
    )
    p.add_argument(
        "--morning-start-hour",
        type=int,
        default=9,
        help="Morning window opens at this hour (0-23). Default: 9",
    )
    p.add_argument(
        "--morning-start-minute",
        type=int,
        default=0,
        help="Minute for morning window open (0-59). Default: 0",
    )
    p.add_argument(
        "--afternoon-start-hour",
        type=int,
        default=15,
        help="Afternoon window starts at this hour (0-23). Default: 15 (use 16 for 4 PM).",
    )
    p.add_argument(
        "--afternoon-start-minute",
        type=int,
        default=0,
        help="Minute for afternoon window start (0-59). Default: 0",
    )
    p.add_argument(
        "--no-catch-up-morning",
        action="store_true",
        help="If morning was missed before afternoon_start, do not run it after afternoon_start; only afternoon runs.",
    )
    p.add_argument("--log-file", default="", help="Append human-readable lines here (optional).")
    p.add_argument("--dry-run", action="store_true", help="Print plan and exit without running the command.")

    args = p.parse_args(pre_argv)

    for label, h, m in (
        ("morning-start", args.morning_start_hour, args.morning_start_minute),
        ("afternoon-start", args.afternoon_start_hour, args.afternoon_start_minute),
    ):
        if not (0 <= h <= 23 and 0 <= m <= 59):
            p.error(f"{label} hour/minute out of range")

    morning_start_minutes = args.morning_start_hour * 60 + args.morning_start_minute
    afternoon_start_minutes = args.afternoon_start_hour * 60 + args.afternoon_start_minute
    if morning_start_minutes >= afternoon_start_minutes:
        p.error("morning start must be strictly before afternoon start")
    if afternoon_start_minutes >= 24 * 60:
        p.error("afternoon start must be before midnight")

    tz = os.environ.get("TZ", "")
    today, minutes = _local_ymd_and_minutes()
    state_path = os.path.expanduser(args.state_file)
    state = _load_state(state_path)

    morning_done_date = str(state.get("morning_done_date", "") or "")
    afternoon_done_date = str(state.get("afternoon_done_date", "") or "")
    morning_done_today = morning_done_date == today
    afternoon_done_today = afternoon_done_date == today

    catch_up_morning = not args.no_catch_up_morning
    plan = _plan(
        minutes,
        morning_start_minutes,
        afternoon_start_minutes,
        morning_done_today,
        afternoon_done_today,
        catch_up_morning,
    )

    log_path = (args.log_file or "").strip() or None
    if log_path:
        log_path = os.path.expanduser(log_path)

    _log_line(
        log_path,
        f"tick tz={tz!r} today={today} minutes={minutes} "
        f"morning_start={morning_start_minutes} afternoon_start={afternoon_start_minutes} "
        f"morning_done={morning_done_today} afternoon_done={afternoon_done_today} "
        f"plan_morning={plan.run_morning} plan_afternoon={plan.run_afternoon} cmd={cmd!r}",
    )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "tz": tz,
                    "today": today,
                    "minutes_since_midnight": minutes,
                    "morning_start_minutes_since_midnight": morning_start_minutes,
                    "afternoon_start_minutes_since_midnight": afternoon_start_minutes,
                    "morning_done_today": morning_done_today,
                    "afternoon_done_today": afternoon_done_today,
                    "run_morning": plan.run_morning,
                    "run_afternoon": plan.run_afternoon,
                    "command": cmd,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if not plan.run_morning and not plan.run_afternoon:
        return 0

    def run_slot(label: str) -> int:
        _log_line(log_path, f"start {label} -> {cmd!r}")
        env = os.environ.copy()
        env["JOB_AGENT_SLOT"] = label
        proc = subprocess.run(cmd, check=False, env=env)
        _log_line(log_path, f"end {label} exit={proc.returncode}")
        return int(proc.returncode)

    # Mutate a copy; persist only after success.
    new_state = dict(state)

    # If both slots are due (e.g. catch-up after sleep), send one combined digest — not two emails.
    if plan.run_morning and plan.run_afternoon:
        _log_line(log_path, "both slots due -> single combined digest")
        rc = run_slot("digest")
        if rc != 0:
            return rc
        new_state["morning_done_date"] = today
        new_state["afternoon_done_date"] = today
        _atomic_write_json(state_path, new_state)
        return 0

    if plan.run_morning:
        rc = run_slot("morning")
        if rc != 0:
            return rc
        new_state["morning_done_date"] = today
        _atomic_write_json(state_path, new_state)

    if plan.run_afternoon:
        rc = run_slot("afternoon")
        if rc != 0:
            return rc
        new_state["afternoon_done_date"] = today
        _atomic_write_json(state_path, new_state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
