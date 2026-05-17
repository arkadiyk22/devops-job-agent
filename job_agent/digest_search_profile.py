"""Build a compact «search profile» table for digest emails (keywords / scope)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

import pandas as pd

from job_agent.query_build import build_google_browser_queries


def _split_or_phrases(text: str) -> List[str]:
    parts = re.split(r"\s+OR\s+", text.strip(), flags=re.IGNORECASE)
    out: List[str] = []
    for p in parts:
        s = p.strip().strip('"').strip("'")
        if s:
            out.append(s)
    return out


def build_search_profile_rows(cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Rows as (scope, keywords) for the digest email table."""
    rows: List[Tuple[str, str]] = []

    hint = str(cfg.get("location_hint") or "Israel").strip()
    aliases = cfg.get("location_hint_aliases") or []
    if isinstance(aliases, list) and aliases:
        alias_s = ", ".join(str(a).strip() for a in aliases if str(a).strip())
        rows.append(("Location filter", f"{hint} ({alias_s})"))
    else:
        rows.append(("Location filter", hint))

    li = cfg.get("linkedin") if isinstance(cfg.get("linkedin"), dict) else {}
    js = li.get("jobs_search") if isinstance(li.get("jobs_search"), dict) else {}
    li_kw = str(js.get("keywords") or "").strip()
    li_loc = str(js.get("location") or "").strip()
    if li_kw:
        phrases = _split_or_phrases(li_kw)
        kw_cell = " · ".join(phrases) if phrases else li_kw
        if li_loc:
            rows.append((f"LinkedIn Jobs ({li_loc})", kw_cell))
        else:
            rows.append(("LinkedIn Jobs", kw_cell))

    role_focus = cfg.get("role_focus") or []
    if isinstance(role_focus, list) and role_focus:
        rows.append(("Target roles", ", ".join(str(r).strip() for r in role_focus if str(r).strip())))

    sc = cfg.get("scoring") if isinstance(cfg.get("scoring"), dict) else {}
    title_kw = sc.get("keywords") or []
    if isinstance(title_kw, list) and title_kw:
        rows.append(("Title keywords (scoring)", ", ".join(str(k).strip() for k in title_kw if str(k).strip())))
    seniority = sc.get("seniority") or []
    if isinstance(seniority, list) and seniority:
        rows.append(("Seniority (scoring)", ", ".join(str(s).strip() for s in seniority if str(s).strip())))

    try:
        gq = build_google_browser_queries(cfg)
    except Exception:
        gq = []
    for i, q in enumerate(gq, 1):
        rows.append((f"Google web query {i}", q))

    gh = cfg.get("greenhouse_boards") or []
    if isinstance(gh, list) and gh:
        rows.append(("Greenhouse boards", ", ".join(str(b).strip() for b in gh if str(b).strip())))

    lever = cfg.get("lever_sites") or []
    if isinstance(lever, list) and lever:
        rows.append(("Lever sites", ", ".join(str(s).strip() for s in lever if str(s).strip())))

    rss = cfg.get("rss_feeds") or []
    if isinstance(rss, list) and rss:
        rows.append(("RSS feeds", f"{len(rss)} configured"))

    return rows


def build_search_profile_df(cfg: Dict[str, Any]) -> pd.DataFrame:
    rows = build_search_profile_rows(cfg)
    if not rows:
        return pd.DataFrame(columns=["Scope", "Keywords"])
    return pd.DataFrame(rows, columns=["Scope", "Keywords"])
