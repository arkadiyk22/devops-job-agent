"""Build a compact «search profile» table for digest emails (keywords / scope)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

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


_PROFILE_COLUMNS = ("Scope", "Keywords", "Unique added")
_NA_ADDED = "—"


def _fetch_stats_by_site(fetch_stats_df: pd.DataFrame) -> Dict[str, int]:
    if fetch_stats_df is None or fetch_stats_df.empty:
        return {}
    out: Dict[str, int] = {}
    for _, row in fetch_stats_df.iterrows():
        site = str(row.get("Site") or "").strip()
        if not site:
            continue
        added = pd.to_numeric(row.get("Unique added"), errors="coerce")
        out[site] = 0 if pd.isna(added) else int(added)
    return out


def _sum_sites(by_site: Dict[str, int], prefix: str) -> Optional[int]:
    matches = {k: v for k, v in by_site.items() if k.startswith(prefix)}
    if not matches:
        return None
    return sum(matches.values())


def _unique_added_display(scope: str, by_site: Dict[str, int]) -> str:
    s = (scope or "").strip()
    if s.startswith("LinkedIn Jobs"):
        if "LinkedIn (browser)" in by_site:
            return str(by_site["LinkedIn (browser)"])
        return _NA_ADDED
    if s.startswith("Google web query"):
        for label in ("Google (browser web)", "SerpAPI: Google web (site: ATS / LinkedIn)"):
            if label in by_site:
                return str(by_site[label])
        return _NA_ADDED
    if s == "Greenhouse boards":
        total = _sum_sites(by_site, "Greenhouse:")
        return str(total) if total is not None else _NA_ADDED
    if s == "Lever sites":
        total = _sum_sites(by_site, "Lever:")
        return str(total) if total is not None else _NA_ADDED
    if s == "RSS feeds":
        total = _sum_sites(by_site, "RSS:")
        return str(total) if total is not None else _NA_ADDED
    return _NA_ADDED


def _consumed_sites(scope: str, by_site: Dict[str, int]) -> Set[str]:
    s = (scope or "").strip()
    consumed: Set[str] = set()
    if s.startswith("LinkedIn Jobs") and "LinkedIn (browser)" in by_site:
        consumed.add("LinkedIn (browser)")
    if s.startswith("Google web query"):
        for label in ("Google (browser web)", "SerpAPI: Google web (site: ATS / LinkedIn)"):
            if label in by_site:
                consumed.add(label)
    if s == "Greenhouse boards":
        consumed.update(k for k in by_site if k.startswith("Greenhouse:"))
    if s == "Lever sites":
        consumed.update(k for k in by_site if k.startswith("Lever:"))
    if s == "RSS feeds":
        consumed.update(k for k in by_site if k.startswith("RSS:"))
    return consumed


def build_search_profile_with_fetch_stats_df(
    cfg: Dict[str, Any],
    fetch_stats_df: pd.DataFrame | None,
) -> pd.DataFrame:
    """
    Search profile + per-source «Unique added» from this run (replaces separate Sources table).
    """
    profile = build_search_profile_df(cfg)
    by_site = _fetch_stats_by_site(fetch_stats_df if fetch_stats_df is not None else pd.DataFrame())
    if profile.empty:
        profile = pd.DataFrame(columns=list(_PROFILE_COLUMNS))

    if "Unique added" not in profile.columns:
        profile["Unique added"] = _NA_ADDED

    consumed: Set[str] = set()
    if not profile.empty:
        added_col: List[str] = []
        for scope in profile["Scope"].astype(str):
            added_col.append(_unique_added_display(scope, by_site))
            consumed |= _consumed_sites(scope, by_site)
        profile["Unique added"] = added_col

    extra_rows: List[Dict[str, str]] = []
    for site, count in sorted(by_site.items()):
        if site in consumed:
            continue
        extra_rows.append({"Scope": site, "Keywords": _NA_ADDED, "Unique added": str(count)})
    if extra_rows:
        profile = pd.concat([profile, pd.DataFrame(extra_rows)], ignore_index=True)

    return profile.reindex(columns=list(_PROFILE_COLUMNS), fill_value=_NA_ADDED)
