"""Fetch job title/company/location from public job URLs (HTTP, no browser)."""

from __future__ import annotations

import re
from typing import Any, Dict
from urllib.request import Request, urlopen

import requests

from job_agent.linkedin_og import fetch_linkedin_og_details_http
from job_agent.sources.greenhouse import _greenhouse_location
from job_agent.util import strip_html

_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
_OG_TITLE_RE_ALT = re.compile(
    r'content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
    re.I,
)
_GREENHOUSE_JOB_RE = re.compile(
    r"boards(?:\.[a-z]{2,3})?\.greenhouse\.io/([^/]+)/jobs/(\d+)",
    re.I,
)


def _fetch_html(url: str, *, timeout: int = 20) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_og_title(html: str) -> str:
    for pat in (_OG_TITLE_RE, _OG_TITLE_RE_ALT):
        m = pat.search(html or "")
        if m:
            return m.group(1).strip()
    return ""


def fetch_greenhouse_job_details_http(link: str) -> Dict[str, str]:
    m = _GREENHOUSE_JOB_RE.search(link or "")
    if not m:
        return {}
    board, job_id = m.group(1), m.group(2)
    api = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
    try:
        r = requests.get(api, timeout=20)
        if r.status_code != 200:
            return {}
        job = r.json()
    except (requests.RequestException, ValueError):
        return {}
    if not isinstance(job, dict):
        return {}
    title = str(job.get("title") or "").strip()
    company = str(job.get("company_name") or board).replace("-", " ").strip()
    if not company or company.lower() == board.lower():
        company = board.replace("-", " ").title()
    loc = _greenhouse_location(job)
    return {
        "title": title[:300],
        "company": company[:120],
        "location": loc[:120],
        "source": f"greenhouse:{board}",
    }


def fetch_job_page_details_http(link: str) -> Dict[str, str]:
    """Best-effort title/company/location for a job posting URL."""
    url = (link or "").split("?")[0]
    low = url.lower()
    if not url.startswith("http"):
        return {}

    if "linkedin.com" in low:
        details = fetch_linkedin_og_details_http(url)
        if details:
            details.setdefault("source", "linkedin_browser")
        return details

    if "greenhouse.io" in low:
        return fetch_greenhouse_job_details_http(url)

    if "jobs.lever.co" in low or "lever.co" in low:
        try:
            html = _fetch_html(url)
            og = _parse_og_title(html)
            if og and " at " in og:
                title, company = og.split(" at ", 1)
                return {
                    "title": title.strip()[:300],
                    "company": company.split("|")[0].strip()[:120],
                    "location": "",
                    "source": "lever",
                }
        except OSError:
            pass

    try:
        html = _fetch_html(url)
        og = _parse_og_title(html)
        if og:
            return {"title": og[:300], "company": "", "location": "", "source": ""}
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        if m:
            title = strip_html(m.group(1)).strip()
            if title and "linkedin" not in title.lower():
                return {"title": title[:300], "company": "", "location": "", "source": ""}
    except OSError:
        pass
    return {}


def fallback_title_for_link(link: str) -> str:
    low = (link or "").lower()
    m = re.search(r"/jobs/view/(?:[^/]+-)?(\d+)", link or "")
    if m:
        return f"LinkedIn job {m.group(1)}"
    gh = _GREENHOUSE_JOB_RE.search(link or "")
    if gh:
        return f"Greenhouse job {gh.group(2)} ({gh.group(1)})"
    return "Job (open link — see Link column)"
