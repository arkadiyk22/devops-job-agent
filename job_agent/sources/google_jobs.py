from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional

from job_agent.models import Job
from job_agent.scoring import score_title
from job_agent.serpapi_client import serpapi_request
from job_agent.settings import get_setting
from job_agent.util import normalize_url


def _looks_configured(api_key: str) -> bool:
    if not api_key:
        return False
    low = api_key.strip().lower()
    if low.startswith("your_") or low in ("changeme", "xxx", "placeholder"):
        return False
    return True


def _merge_serpapi_params(base: dict[str, str], cfg: Dict[str, Any]) -> dict[str, str]:
    extra = cfg.get("serpapi_google_jobs_params")
    if not isinstance(extra, dict):
        return base
    out = dict(base)
    for k, v in extra.items():
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out[str(k)] = s
    return out


def _serpapi_google_jobs_params(q: str, api_key: str, cfg: Dict[str, Any], hl_override: Optional[str] = None) -> dict[str, str]:
    """Build SerpAPI google_jobs params; pin geography when location_hint / serpapi_* are set."""
    params: dict[str, str] = {"engine": "google_jobs", "q": q, "api_key": api_key}
    loc = (cfg.get("serpapi_location") or cfg.get("location_hint") or "").strip()
    if loc:
        params["location"] = loc
    gl = (cfg.get("serpapi_gl") or "").strip()
    if gl:
        params["gl"] = gl
    gd = (cfg.get("serpapi_google_domain") or "").strip()
    if gd:
        params["google_domain"] = gd
    hl = (hl_override if hl_override is not None else (cfg.get("serpapi_hl") or "")).strip()
    if hl:
        params["hl"] = hl
    return _merge_serpapi_params(params, cfg)


def _posted_display_from_serp_job(job: Dict[str, Any]) -> str:
    ext = job.get("detected_extensions") or {}
    if isinstance(ext, dict):
        pa = ext.get("posted_at")
        if pa is not None and str(pa).strip():
            return str(pa).strip()
    for e in job.get("extensions") or []:
        if isinstance(e, str):
            es = e.strip()
            if not es:
                continue
            el = es.lower()
            if any(x in el for x in ("ago", "hour", "day", "week", "month", "today", "yesterday")):
                return es
            if "ימים" in es or "שבוע" in es:
                return es
    return "recent"


def _serpapi_google_jobs_once(params: dict) -> dict:
    return serpapi_request(params)


def _serpapi_google_jobs_retry(params: dict) -> dict:
    last: Exception | None = None
    for attempt in range(3):
        try:
            return _serpapi_google_jobs_once(params)
        except RuntimeError as e:
            msg = str(e)
            if "401" in msg or "403" in msg or "Invalid API key" in msg:
                raise
            if "429" in msg or "run out of searches" in msg.lower():
                raise
            if _is_no_jobs_for_query(msg):
                raise
            last = e
            if attempt < 2:
                time.sleep(min(30, 2 ** (attempt + 1)))
    assert last is not None
    raise last


def _is_no_jobs_for_query(msg: str) -> bool:
    """SerpAPI returns HTTP 200 + error field when Google has no hits — not a fatal key failure."""
    m = msg.lower()
    return (
        "hasn't returned any results" in m
        or "has not returned any results" in m
        or "no results for this query" in m
    )


def _google_jobs_hl_variants(cfg: Dict[str, Any]) -> List[str]:
    raw = cfg.get("serpapi_google_jobs_hl_variants")
    if isinstance(raw, list) and raw:
        out = [str(h).strip() for h in raw if str(h).strip()]
        return out if out else ["en"]
    primary = (cfg.get("serpapi_hl") or "").strip()
    return [primary] if primary else ["en"]


def fetch_google_jobs(queries: List[str], cfg: Dict[str, Any]) -> List[Job]:
    api_key = (get_setting("SERPAPI_KEY", "GOOGLE_JOBS_API_KEY") or "").strip()
    if not _looks_configured(api_key):
        print("SerpAPI Google Jobs: skipped (set a real SERPAPI_KEY in .env)", file=sys.stderr)
        return []

    out: List[Job] = []
    seen: set[str] = set()

    hl_variants = _google_jobs_hl_variants(cfg)

    for q in queries:
        for hl_v in hl_variants:
            params = _serpapi_google_jobs_params(q, api_key, cfg, hl_override=hl_v)
            try:
                data = _serpapi_google_jobs_retry(params)
            except RuntimeError as e:
                msg = str(e)
                if "429" in msg or "run out of searches" in msg.lower():
                    print(
                        "SerpAPI Google Jobs: quota exhausted (HTTP 429) — stopping Google Jobs fetch "
                        f"({len(out)} jobs collected so far).",
                        file=sys.stderr,
                    )
                    return out
                if "401" in msg or "403" in msg or "Invalid API key" in msg:
                    print(
                        "SerpAPI Google Jobs: invalid or unauthorized API key — skipping SerpAPI jobs.",
                        file=sys.stderr,
                    )
                    return []
                if _is_no_jobs_for_query(msg):
                    print(
                        f"SerpAPI Google Jobs: no results for query {q!r} (hl={params.get('hl', '')}) — skipping.",
                        file=sys.stderr,
                    )
                    continue
                raise

            for job in data.get("jobs_results") or []:
                title = job.get("title", "") or ""
                opts = job.get("apply_options") or [{}]
                link = (opts[0] or {}).get("link", "") if opts else ""
                if not link:
                    continue
                link_n = normalize_url(link)
                if link_n in seen:
                    continue
                seen.add(link_n)
                raw = dict(job) if isinstance(job, dict) else {}
                out.append(
                    Job(
                        source="serpapi_google_jobs",
                        company=job.get("company_name", "") or "",
                        title=title,
                        location=job.get("location", "") or "",
                        link=link_n,
                        posted=_posted_display_from_serp_job(job),
                        score=score_title(title, cfg),
                        raw=raw,
                    )
                )
    return out
