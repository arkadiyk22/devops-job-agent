from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

_DEFAULT_GENIE = Path.home() / "genie4cv" / "local.settings.json"
_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(_ROOT / ".env", override=False)
    except ImportError:
        pass


def settings_path() -> Path:
    return Path(os.getenv("GENIE4CV_SETTINGS", str(_DEFAULT_GENIE)))


def load_genie_values() -> Dict[str, Any]:
    p = settings_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data.get("Values", {}) if isinstance(data, dict) else {}


_load_dotenv()
SETTINGS: Dict[str, Any] = load_genie_values()


def get_setting(*keys: str, default: str = "") -> str:
    """Env wins, then Genie4CV Values JSON."""
    for key in keys:
        v = os.getenv(key) or SETTINGS.get(key)
        if v:
            return str(v)
    return default


def _serpapi_env_brake_on() -> bool:
    return (os.getenv("JOB_AGENT_NO_SERPAPI") or "").strip().lower() in ("1", "true", "yes", "on")


_BUILTIN_SERPAPI_FEATURES = frozenset({"google_jobs", "google_site_ats", "contacts"})


def serpapi_feature_enabled(feature: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Whether a **named** SerpAPI feature may run (per ``serpapi_features`` or legacy ``use_serpapi``).

    - If ``JOB_AGENT_NO_SERPAPI`` is set, everything is off.
    - If ``serpapi_features`` exists in config (dict): only keys set to ``true`` run (including custom keys
      for your own code — use :func:`job_agent.serpapi_optional.serpapi_try`).
    - If ``serpapi_features`` is absent: legacy behaviour — ``use_serpapi: true`` enables all built-in
      features (``google_jobs``, ``google_site_ats``, ``contacts``); ``use_serpapi: false`` disables all.
    """
    if _serpapi_env_brake_on():
        return False
    if not isinstance(cfg, dict):
        return False
    if "serpapi_features" in cfg and isinstance(cfg["serpapi_features"], dict):
        return bool(cfg["serpapi_features"].get(feature, False))
    return bool(cfg.get("use_serpapi", False)) and feature in _BUILTIN_SERPAPI_FEATURES


def serpapi_any_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True if any SerpAPI usage is allowed (used to avoid pointless --sources warnings)."""
    if _serpapi_env_brake_on():
        return False
    if not isinstance(cfg, dict):
        return False
    if "serpapi_features" in cfg and isinstance(cfg["serpapi_features"], dict):
        return any(bool(v) for v in cfg["serpapi_features"].values())
    return bool(cfg.get("use_serpapi", False))


def serpapi_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Alias for :func:`serpapi_any_enabled` (backward compatible name)."""
    return serpapi_any_enabled(cfg)
