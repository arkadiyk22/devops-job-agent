#!/usr/bin/env python3
"""CLI entry: python run.py [--dry-run] [--allow-non-israel-email] [--skip-contacts] [--sources ...]"""

import sys

try:
    from job_agent.main import run
except ImportError as exc:
    print(
        "Missing Python dependency (install project requirements first).\n"
        f"  {exc}\n"
        "Fix:\n"
        "  python3 -m pip install -r requirements.txt\n"
        "Or use a venv:  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc

if __name__ == "__main__":
    raise SystemExit(run())
