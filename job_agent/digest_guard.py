"""Prevent duplicate digest emails within a short window (manual run + launchd overlap)."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple


def _state_path(cfg: Dict[str, Any]) -> Path:
    raw = str(cfg.get("digest_send_state_file") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    return Path.home() / ".job-agent" / ".last-digest-send.json"


def _load(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def minutes_since_last_send(cfg: Dict[str, Any]) -> float | None:
    path = _state_path(cfg)
    state = _load(path)
    ts = state.get("sent_at_unix")
    if ts is None:
        return None
    try:
        return max(0.0, (time.time() - float(ts)) / 60.0)
    except (TypeError, ValueError):
        return None


def should_skip_send(cfg: Dict[str, Any], *, slot: str = "") -> Tuple[bool, str]:
    """True if a digest was sent recently (same cooldown for all slots)."""
    min_gap = float(cfg.get("digest_min_minutes_between_sends") or 90)
    if min_gap <= 0:
        return False, ""
    ago = minutes_since_last_send(cfg)
    if ago is None or ago >= min_gap:
        return False, ""
    return True, (
        f"Skipping digest send ({slot or 'digest'}): last email was {ago:.0f} min ago "
        f"(cooldown {min_gap:.0f} min). One combined digest per window."
    )


def record_send(cfg: Dict[str, Any], *, slot: str, job_count: int) -> None:
    path = _state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sent_at_unix": time.time(),
        "sent_at_iso": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "slot": slot,
        "job_count": job_count,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
