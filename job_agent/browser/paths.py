from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict


def resolve_browser_user_data_dir(cfg: Dict[str, Any], *, service: str = "linkedin") -> Path:
    """Persistent Chromium profile (cookies / login). Default: ~/.job-agent/browser/<service>."""
    block = cfg.get("browser")
    if isinstance(block, dict):
        raw = (block.get("user_data_dir") or "").strip()
        if raw:
            base = Path(os.path.expanduser(raw))
        else:
            base = Path.home() / ".job-agent" / "browser"
        if service and not raw.endswith(service):
            return base / service if base.name != service else base
        return base
    return Path.home() / ".job-agent" / "browser" / service
