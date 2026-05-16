"""Israel / location_hint filtering (v1 strict aliases + v2 israel_or_il_signals)."""

from __future__ import annotations

from typing import Any, Dict, List

from job_agent.models import Job


def _location_aliases(cfg: Dict[str, Any]) -> List[str]:
    hint = (cfg.get("location_hint") or "").strip()
    if not hint:
        return []
    needle = hint.lower()
    aliases = [needle]
    for a in cfg.get("location_hint_aliases") or []:
        t = str(a).strip().lower()
        if t and t not in aliases:
            aliases.append(t)
    return aliases


def _has_alias(text: str, aliases: List[str]) -> bool:
    low = (text or "").lower()
    return any(a in low for a in aliases)


def _israel_or_il_signals_match(j: Job, cfg: Dict[str, Any], aliases: List[str]) -> bool:
    """Pass rows with Israel text, IL host signals, or trusted browser LinkedIn search."""
    loc = j.location or ""
    title = j.title or ""
    company = j.company or ""
    link = (j.link or "").lower()
    strict_loc_title = bool(cfg.get("location_hint_strict_location_or_title", False))

    if strict_loc_title:
        if _has_alias(f"{loc} {title}", aliases):
            return True
    elif _has_alias(f"{loc} {title} {company}", aliases):
        return True

    if "il.linkedin.com" in link or ".co.il" in link:
        return True

    sl = j.source.lower()
    if sl == "linkedin_browser":
        raw = j.raw if isinstance(j.raw, dict) else {}
        search_url = str(raw.get("search_url") or "").lower()
        if "israel" in search_url or "ישראל" in search_url:
            return True
        if _has_alias(loc, aliases) or _has_alias(title, aliases):
            return True
        return True

    if sl.startswith("google_browser:"):
        raw = j.raw if isinstance(j.raw, dict) else {}
        gq = str(raw.get("google_query") or "").lower()
        if "israel" in gq or "ישראל" in gq:
            if _has_alias(loc, aliases) or _has_alias(title, aliases) or not loc.strip():
                return True
        link_low = link
        if "il.linkedin.com" in link_low or ".co.il" in link_low:
            return True
        if _has_alias(f"{loc} {title}", aliases):
            return True
        return False

    if sl.startswith("rss:") and (loc.strip().lower() == "israel" or _has_alias(loc, aliases)):
        return True

    include_desc = bool(cfg.get("location_hint_include_job_description", False)) and not strict_loc_title
    if include_desc and isinstance(j.raw, dict):
        desc_cap = int(cfg.get("location_hint_description_max_chars") or 4000)
        txt = str(j.raw.get("text") or "")[: max(500, desc_cap)]
        if txt and _has_alias(txt, aliases):
            return True

    return False


def filter_jobs_by_location_hint(jobs: List[Job], cfg: Dict[str, Any]) -> List[Job]:
    """Keep rows that look Israel-related per config."""
    if not cfg.get("filter_jobs_by_location_hint", False):
        return jobs
    if not (cfg.get("location_hint") or "").strip():
        return jobs

    raw = cfg.get("location_filter_source_prefixes")
    if raw is None:
        prefixes = ("greenhouse:", "lever:", "rss:", "linkedin_browser")
    elif isinstance(raw, list) and len(raw) == 0:
        return jobs
    else:
        prefixes = tuple(str(x).lower() for x in raw)

    mode = str(cfg.get("location_filter_mode") or "").strip().lower()
    aliases = _location_aliases(cfg)
    out: List[Job] = []

    for j in jobs:
        sl = j.source.lower()
        if not any(sl.startswith(p) for p in prefixes):
            out.append(j)
            continue

        if mode == "israel_or_il_signals":
            if _israel_or_il_signals_match(j, cfg, aliases):
                out.append(j)
            continue

        strict_loc_title = bool(cfg.get("location_hint_strict_location_or_title", False))
        include_desc = bool(cfg.get("location_hint_include_job_description", False)) and not strict_loc_title
        desc_cap = int(cfg.get("location_hint_description_max_chars") or 4000)
        if desc_cap < 500:
            desc_cap = 500
        if desc_cap > 50000:
            desc_cap = 50000

        loc = j.location or ""
        title = j.title or ""
        company = j.company or ""
        if strict_loc_title:
            if _has_alias(f"{loc} {title}", aliases):
                out.append(j)
            continue
        if _has_alias(f"{loc} {title} {company}", aliases):
            out.append(j)
            continue
        if include_desc and isinstance(j.raw, dict):
            txt = str(j.raw.get("text") or "")
            if txt and _has_alias(txt[:desc_cap], aliases):
                out.append(j)

    return out
