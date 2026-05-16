"""How job discovery runs: legacy SerpAPI (v1) vs browser session (v2)."""

from __future__ import annotations

from typing import Any, Dict


def uses_browser_search(cfg: Dict[str, Any]) -> bool:
    """True when config opts into logged-in browser sources (no SerpAPI required)."""
    return str(cfg.get("search_mode") or "").strip().lower() == "browser"
