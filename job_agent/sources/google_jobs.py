from __future__ import annotations

import sys
import time
from typing import Any, Dict, List

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


def _serpapi_google_jobs_params(q: str, api_key: str, cfg: Dict[str, Any]) -> dict:
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
    hl = (cfg.get("serpapi_hl") or "").strip()
    if hl:
        params["hl"] = hl
    return params


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


def fetch_google_jobs(queries: List[str], cfg: Dict[str, Any]) -> List[Job]:
    api_key = (get_setting("SERPAPI_KEY", "GOOGLE_JOBS_API_KEY") or "").strip()
    if not _looks_configured(api_key):
        print("SerpAPI Google Jobs: skipped (set a real SERPAPI_KEY in .env)", file=sys.stderr)
        return []

    out: List[Job] = []
    seen: set[str] = set()

    for q in queries:
        params = _serpapi_google_jobs_params(q, api_key, cfg)
        try:
            data = _serpapi_google_jobs_retry(params)
        except RuntimeError as e:
            msg = str(e)
            if "401" in msg or "403" in msg or "Invalid API key" in msg:
                print(
                    "SerpAPI Google Jobs: invalid or unauthorized API key — skipping SerpAPI jobs.",
                    file=sys.stderr,
                )
                return []
            if _is_no_jobs_for_query(msg):
                print(f"SerpAPI Google Jobs: no results for query {q!r} — skipping.", file=sys.stderr)
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
            out.append(
                Job(
                    source="serpapi_google_jobs",
                    company=job.get("company_name", "") or "",
                    title=title,
                    location=job.get("location", "") or "",
                    link=link_n,
                    posted=str((job.get("detected_extensions") or {}).get("posted_at", "recent")),
                    score=score_title(title, cfg),
                )
            )
    return out
