"""SerpAPI Google Web search: ``site:<ats-host>`` queries for ATS / career pages."""

from __future__ import annotations

import re
import sys
import time
from typing import Any, Dict, List
from urllib.parse import urlparse

from job_agent.models import Job
from job_agent.scoring import score_title
from job_agent.serpapi_client import serpapi_request
from job_agent.settings import get_setting
from job_agent.sources.google_jobs import _is_no_jobs_for_query, _looks_configured
from job_agent.linkedin_og import is_linkedin_post_url, split_linkedin_google_result
from job_agent.util import normalize_url


def _serpapi_google_web_retry(params: dict) -> dict:
    last: Exception | None = None
    for attempt in range(3):
        try:
            return serpapi_request(params)
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


def _google_web_params(q: str, api_key: str, cfg: Dict[str, Any], num: int) -> dict[str, str]:
    params: dict[str, str] = {"engine": "google", "q": q, "api_key": api_key}
    gd = (cfg.get("serpapi_google_domain") or "").strip()
    if gd:
        params["google_domain"] = gd
    gl = (cfg.get("serpapi_gl") or "").strip()
    if gl:
        params["gl"] = gl
    hl = (cfg.get("serpapi_hl") or "").strip()
    if hl:
        params["hl"] = hl
    if num > 0:
        params["num"] = str(min(num, 100))

    extra = cfg.get("serpapi_google_web_params")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is None:
                continue
            s = str(v).strip()
            if s:
                params[str(k)] = s
    return params


def _site_label_from_query(q: str) -> str:
    m = re.search(r"site:([^\s]+)", q, flags=re.I)
    if not m:
        return "ats"
    h = m.group(1).strip().lower()
    if "linkedin.com/posts" in q.lower() or re.search(r"site:\s*[\w.]*linkedin\.com/posts", q, re.I):
        return "linkedin_post"
    for name in ("comeet", "greenhouse", "workday", "lever", "workable", "smartrecruiters", "linkedin"):
        if name in h:
            return name
    return h.split(".")[0] if h else "ats"


def _company_from_path(link: str) -> str:
    try:
        u = urlparse(link)
    except ValueError:
        return ""
    host = (u.netloc or "").lower()
    parts = [p for p in u.path.split("/") if p and p not in ("jobs", "job", "vacancy", "j", "wd", "listing")]
    if not parts:
        return ""
    if "greenhouse" in host:
        return parts[0].replace("-", " ").title()
    if "lever.co" in host:
        return parts[0].replace("-", " ").title()
    if "comeet" in host and len(parts) >= 1:
        return parts[0].replace("-", " ").title()
    if "workable" in host and len(parts) >= 1:
        return parts[0].replace("-", " ").title()
    if "smartrecruiters" in host:
        return parts[0].replace("-", " ").title() if parts else ""
    if "myworkdayjobs.com" in host and len(parts) >= 1:
        return parts[0].replace("-", " ").title()
    if "linkedin.com" in host:
        if len(parts) >= 1 and parts[0] not in ("jobs", "job", "view", "posts", "feed"):
            return parts[0].replace("-", " ").title()
    return ""


def _clean_organic_title(title: str) -> str:
    t = (title or "").strip()
    return t[:300] or "Role"


def _split_title_company(title: str, snippet: str, link: str) -> tuple[str, str]:
    if is_linkedin_post_url(link) or (
        "linkedin.com" in (link or "").lower() and " hiring " in f" {(title or '').lower()} "
    ):
        t, co, _loc = split_linkedin_google_result(title, snippet, link)
        if t and co:
            return _clean_organic_title(t), co[:120]
    raw = (title or "").strip()
    company = ""
    for sep in (" | ", " – ", " — "):
        if sep in raw:
            bits = [x.strip() for x in raw.split(sep)]
            if len(bits) >= 2:
                company = bits[-1]
                raw = sep.join(bits[:-1]).strip()
                break
    if not company:
        company = _company_from_path(link)
    if not company and snippet:
        line = snippet.strip().split("\n")[0][:120]
        if " - " in line:
            company = line.split(" - ")[-1].strip()[:80]
    return _clean_organic_title(raw), (company or "Unknown")[:120]


def _is_probable_job_url(link: str) -> bool:
    low = (link or "").lower()
    if not low.startswith("http"):
        return False
    if "google.com/search" in low or "/search?q=" in low:
        return False
    try:
        u = urlparse(link)
    except ValueError:
        return False
    path = u.path.lower()

    if "greenhouse.io" in low or "boards.greenhouse" in low:
        return bool(re.search(r"/jobs/\d+", low))
    if "lever.co" in low:
        return "jobs.lever.co" in low
    if "comeet.co" in low or "comeet.com" in low:
        return "/jobs/" in path or "/job/" in path
    if "myworkdayjobs.com" in low:
        return "/job/" in path or re.search(r"/wd\d+/", low) is not None
    if "workable.com" in low:
        return "/j/" in path
    if "smartrecruiters.com" in low:
        return "/vacancy/" in path or "/job/" in path or re.search(r"/\d{7,}/", path) is not None
    if "linkedin.com" in low:
        if "/jobs/view/" in path or re.search(r"/jobs/view/\d", low):
            return True
        if is_linkedin_post_url(link):
            return True
        return False
    return False


def fetch_google_site_ats(queries: List[str], cfg: Dict[str, Any]) -> List[Job]:
    api_key = (get_setting("SERPAPI_KEY", "GOOGLE_JOBS_API_KEY") or "").strip()
    if not _looks_configured(api_key):
        print("SerpAPI Google site:ATS: skipped (set SERPAPI_KEY in .env)", file=sys.stderr)
        return []
    if not queries:
        return []

    block = cfg.get("ats_google_site_search")
    block = block if isinstance(block, dict) else {}
    num = int(block.get("num") or 15)
    delay = float(block.get("request_delay_seconds") or 0.35)

    out: List[Job] = []
    seen: set[str] = set()
    log_q = bool(cfg.get("serpapi_log_each_query", False))
    nq = len(queries)

    for i, q in enumerate(queries):
        if log_q:
            print(
                f"SerpAPI Google site:ATS: request {i + 1}/{nq} q={q!r}",
                file=sys.stderr,
            )
        params = _google_web_params(q, api_key, cfg, num)
        try:
            data = _serpapi_google_web_retry(params)
        except RuntimeError as e:
            msg = str(e)
            if "401" in msg or "403" in msg or "Invalid API key" in msg:
                print("SerpAPI Google site:ATS: invalid API key — stopping.", file=sys.stderr)
                return out
            if "429" in msg or "run out of searches" in msg.lower():
                print(
                    "SerpAPI Google site:ATS: quota exhausted (HTTP 429) — stopping site:ATS fetch "
                    f"({len(out)} jobs collected).",
                    file=sys.stderr,
                )
                return out
            if _is_no_jobs_for_query(msg):
                print(f"SerpAPI Google site:ATS: no results for query {q!r} — skipping.", file=sys.stderr)
                continue
            raise

        label = _site_label_from_query(q)
        organic = data.get("organic_results") or []
        if not organic and log_q:
            print(
                f"SerpAPI Google site:ATS: empty organic_results for query {q!r} — skipping.",
                file=sys.stderr,
            )
        n_jobs_before = len(out)
        for row in organic:
            if not isinstance(row, dict):
                continue
            link = (row.get("link") or "").strip()
            if not link or not _is_probable_job_url(link):
                continue
            link_n = normalize_url(link)
            if link_n in seen:
                continue
            seen.add(link_n)
            title_raw = row.get("title") or ""
            snippet = row.get("snippet") or ""
            title, company = _split_title_company(str(title_raw), str(snippet), link_n)
            raw: Dict[str, Any] = {
                "description": str(snippet),
                "text": str(snippet),
                "google_query": q,
            }
            out.append(
                Job(
                    source=f"google_site_ats:{label}",
                    company=company,
                    title=title,
                    location="",
                    link=link_n,
                    posted="recent",
                    score=score_title(title, cfg),
                    raw=raw,
                )
            )
        if log_q and len(out) == n_jobs_before and organic:
            print(
                f"SerpAPI Google site:ATS: no job URLs matched filters for query {q!r} — skipping.",
                file=sys.stderr,
            )

        if delay > 0 and i + 1 < len(queries):
            time.sleep(delay)

    return out
