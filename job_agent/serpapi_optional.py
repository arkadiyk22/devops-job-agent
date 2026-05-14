"""Call SerpAPI only when a named feature is enabled in ``config.json``.

Use this from **your own modules** so SerpAPI runs only where you opt in:

1. Add a flag under ``serpapi_features`` (any string key, e.g. ``"my_scraper": true``).
2. Call ``serpapi_try`` / ``serpapi_search`` with the same ``feature`` name.

Built-in features used by the agent: ``google_jobs``, ``google_site_ats``, ``contacts``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from job_agent.serpapi_client import serpapi_request
from job_agent.settings import get_setting, serpapi_feature_enabled


def _api_key() -> str:
    return (get_setting("SERPAPI_KEY", "GOOGLE_JOBS_API_KEY") or "").strip()


def serpapi_try(
    feature: str,
    params: Dict[str, Any],
    cfg: Dict[str, Any],
    *,
    inject_api_key: bool = True,
) -> Optional[Dict[str, Any]]:
    """Run SerpAPI only if ``serpapi_features.<feature>`` is true (or legacy ``use_serpapi`` for built-ins).

    Returns ``None`` if the feature is off or the API key is missing.
    """
    if not serpapi_feature_enabled(feature, cfg):
        return None
    key = _api_key()
    if not key or key.lower().startswith("your_") or key.lower() in ("changeme", "xxx", "placeholder"):
        return None
    p = dict(params)
    if inject_api_key:
        p["api_key"] = key
    return serpapi_request(p)


def serpapi_search(
    feature: str,
    params: Dict[str, Any],
    cfg: Dict[str, Any],
    *,
    inject_api_key: bool = True,
) -> Dict[str, Any]:
    """Same as ``serpapi_try`` but raises if disabled or misconfigured."""
    out = serpapi_try(feature, params, cfg, inject_api_key=inject_api_key)
    if out is None:
        raise RuntimeError(
            f"SerpAPI feature {feature!r} is disabled or SERPAPI_KEY is missing. "
            f"Set serpapi_features.{feature} to true in config.json (and a valid key in .env)."
        )
    return out
