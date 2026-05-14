"""Filter job rows (closed postings, max posting age) per config templates."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from job_agent.models import Job

_DEFAULT_CLOSED = (
    "no longer accepting",
    "no longer accepting applications",
    "not accepting applications",
    "applications closed",
    "position filled",
    "role filled",
    "expired",
    "this job is no longer",
    "סגור להגשה",
    "סגורה להגשה",
    "לא מקבלים מועמדויות",
    "הגשה נסגרה",
    "המשרה אויספה",
    "המשרה נסגרה",
    "אין צורך בעוד",
    "לא פעיל",
)


def _closed_phrases(cfg: Dict[str, Any]) -> List[str]:
    extra = cfg.get("closed_application_phrases") or []
    base = list(_DEFAULT_CLOSED)
    if isinstance(extra, list):
        base.extend(str(x).strip().lower() for x in extra if str(x).strip())
    return base


def is_closed_application(text: str, cfg: Dict[str, Any]) -> bool:
    t = (text or "").lower()
    if not t.strip():
        return False
    for phrase in _closed_phrases(cfg):
        if phrase and phrase in t:
            return True
    return False


def _days_since_datetime(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt.astimezone(timezone.utc)
    return max(0, delta.days)


def _posted_days_from_string(posted: str) -> Optional[int]:
    """Return approximate age in days, or None if unknown."""
    raw = (posted or "").strip()
    p = raw.lower()
    if not p or p == "recent":
        return None

    # ISO-8601 (Greenhouse/Lever/RSS normalized)
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            iso = raw.replace("Z", "+00:00")
            if "T" in iso:
                dt = datetime.fromisoformat(iso)
            else:
                dt = datetime.fromisoformat(raw[:10]).replace(tzinfo=timezone.utc)
            return _days_since_datetime(dt)
        except ValueError:
            pass

    # RFC 2822 (RSS)
    try:
        dt = parsedate_to_datetime(raw)
        return _days_since_datetime(dt)
    except (TypeError, ValueError):
        pass

    m = re.search(r"(\d+)\s*days?\s*ago", p)
    if m:
        return int(m.group(1))

    m = re.search(r"לפני\s*(\d+)\s*ימים", raw)
    if m:
        return int(m.group(1))

    m = re.search(r"לפני\s*שבועיים", raw)
    if m:
        return 14
    m = re.search(r"לפני\s*שבוע", raw)
    if m:
        return 7
    m = re.search(r"לפני\s*(\d+)\s*שבועות?", raw)
    if m:
        return int(m.group(1)) * 7

    if "hour" in p or "minute" in p or "just posted" in p or p == "today":
        return 0
    if "yesterday" in p:
        return 1

    m = re.search(r"(\d+)\s*weeks?\s*ago", p)
    if m:
        return int(m.group(1)) * 7

    m = re.search(r"(\d+)\s*months?\s*ago", p)
    if m:
        return int(m.group(1)) * 30

    if "week" in p and "ago" in p:
        return 7
    if "month" in p and "ago" in p:
        return 30

    return None


def posted_within_max_days(posted: str, max_days: int, *, keep_unknown: bool) -> bool:
    if max_days is None or max_days <= 0:
        return True
    days = _posted_days_from_string(posted)
    if days is None:
        return keep_unknown
    return days <= max_days


def job_text_for_filters(job: Job) -> str:
    parts = [job.title, job.company, job.location, job.link]
    raw = job.raw if isinstance(job.raw, dict) else {}
    parts.append(str(raw.get("text", "") or ""))
    parts.append(str(raw.get("description", "") or ""))
    for h in raw.get("job_highlights") or []:
        if isinstance(h, dict):
            parts.append(str(h.get("title", "") or ""))
            for it in h.get("items") or []:
                parts.append(str(it))
    return " ".join(parts)


def _default_blocked_domains() -> Tuple[str, ...]:
    return (
        "linkedin.com",
        "lnkd.in",
        "facebook.com",
        "fb.com",
        "twitter.com",
        "x.com",
    )


def google_search_fallback_url(
    title: str, company: str, location: str, *, geo_hint: str = ""
) -> str:
    loc = " ".join(x for x in (location, geo_hint) if x).strip()
    q = " ".join(x for x in (title, company, loc, "jobs") if x).strip()
    from urllib.parse import quote_plus

    return f"https://www.google.com/search?q={quote_plus(q)}"


def annotate_search_fallback_for_blocked_domains(jobs: List[Job], cfg: Dict[str, Any]) -> None:
    """When apply links go to hosts that often block deep links, add a Google search URL."""
    if not cfg.get("enable_search_fallback_for_blocked_domains", True):
        return
    raw = cfg.get("search_fallback_blocked_domains")
    domains: Sequence[str] = raw if isinstance(raw, list) and raw else _default_blocked_domains()
    domains_l = tuple(str(d).lower().strip() for d in domains if str(d).strip())
    geo = (cfg.get("location_hint") or "").strip()
    for j in jobs:
        if (j.search_fallback or "").strip():
            continue
        link = (j.link or "").lower()
        if not any(d in link for d in domains_l):
            continue
        j.search_fallback = google_search_fallback_url(j.title, j.company, j.location, geo_hint=geo)


def _effective_keep_unknown_posted_age(job: Job, cfg: Dict[str, Any]) -> bool:
    """Per global config, or any matching ``keep_unknown_posted_age_for_source_prefixes``."""
    if bool(cfg.get("keep_unknown_posted_age", False)):
        return True
    prefs = cfg.get("keep_unknown_posted_age_for_source_prefixes") or []
    if not isinstance(prefs, list):
        return False
    sl = job.source.lower()
    for p in prefs:
        t = str(p).strip().lower()
        if t and sl.startswith(t):
            return True
    return False


def apply_job_filters(jobs: List[Job], cfg: Dict[str, Any]) -> List[Job]:
    """Drop closed postings and optionally enforce max posting age."""
    max_days = int(cfg.get("max_posted_age_days") or 0)

    out: List[Job] = []
    for j in jobs:
        blob = job_text_for_filters(j)
        if is_closed_application(blob, cfg):
            continue
        ku = _effective_keep_unknown_posted_age(j, cfg)
        if not posted_within_max_days(j.posted, max_days, keep_unknown=ku):
            continue
        out.append(j)
    return out
