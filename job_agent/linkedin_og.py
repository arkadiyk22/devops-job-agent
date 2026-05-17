"""Parse LinkedIn ``og:title`` hiring lines (jobs + feed posts)."""

from __future__ import annotations

import re
from typing import Any, Dict
from urllib.request import Request, urlopen

_HIRING_OG_RE = re.compile(
    r"^(.+?)\s+hiring\s+(.+?)\s+in\s+(.+?)\s*\|\s*LinkedIn\s*$",
    re.I,
)
_OG_TITLE_META_RE = re.compile(
    r'property="og:title"\s+content="([^"]+)"',
    re.I,
)
_LINKEDIN_POST_PATH_RE = re.compile(r"/posts/|/feed/update/", re.I)


def is_linkedin_post_url(link: str) -> bool:
    low = (link or "").lower()
    return "linkedin.com" in low and bool(_LINKEDIN_POST_PATH_RE.search(low))


def is_linkedin_job_view_url(link: str) -> bool:
    low = (link or "").lower()
    return "linkedin.com/jobs" in low and "/jobs/view/" in low


def parse_linkedin_hiring_title(text: str) -> Dict[str, str]:
    """``Company hiring Title in Location | LinkedIn`` (Google/Serp snippets or og:title)."""
    raw = (text or "").strip()
    m = _HIRING_OG_RE.match(raw)
    if not m:
        return {}
    return {
        "company": m.group(1).strip()[:120],
        "title": m.group(2).strip()[:300],
        "location": m.group(3).strip()[:120],
    }


def hiring_signal_in_text(text: str) -> bool:
    t = (text or "").lower()
    if parse_linkedin_hiring_title(text):
        return True
    if re.search(r"\b(hiring|we're hiring|we are hiring|open role|join our team|now hiring)\b", t, re.I):
        return True
    if re.search(r"מגייס|מגייסים|משרה פנויה|דרוש|דרושה|מחפש", text or ""):
        return True
    return False


def matches_leadership_role_focus(text: str, cfg: Dict[str, Any]) -> bool:
    """DevOps / platform / SRE leadership titles (same signals as scoring)."""
    t = (text or "").lower()
    if not t.strip():
        return False
    sc = cfg.get("scoring") if isinstance(cfg.get("scoring"), dict) else {}
    keywords = [str(k).lower() for k in (sc.get("keywords") or ["devops", "platform", "sre", "infrastructure"])]
    seniority = [str(s).lower() for s in (sc.get("seniority") or ["manager", "director", "head", "vp", "lead"])]
    role_focus = [str(r).lower() for r in (cfg.get("role_focus") or []) if str(r).strip()]

    if any(r in t for r in role_focus):
        return True
    if "devops" in t and any(s in t for s in ("manager", "director", "head", "lead", "vp")):
        return True
    if any(k in t for k in keywords) and any(s in t for s in seniority):
        return True
    return False


def fetch_linkedin_og_details_http(link: str) -> Dict[str, str]:
    """Public HTML: ``og:title`` for job view pages and hiring posts."""
    url = (link or "").split("?")[0]
    low = url.lower()
    if not (
        is_linkedin_job_view_url(url)
        or is_linkedin_post_url(url)
        or "linkedin.com" in low
    ):
        return {}
    try:
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
        with urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return {}
    m = _OG_TITLE_META_RE.search(html)
    if not m:
        return {}
    return parse_linkedin_hiring_title(m.group(1))


def split_linkedin_google_result(title: str, snippet: str, link: str) -> tuple[str, str, str]:
    """Return (title, company, location) from a Google organic hit."""
    for blob in (title, snippet):
        parsed = parse_linkedin_hiring_title(blob)
        if parsed:
            return parsed["title"], parsed["company"], parsed["location"]
    og = fetch_linkedin_og_details_http(link)
    if og:
        return og.get("title", ""), og.get("company", ""), og.get("location", "")
    return (title or "Role")[:300], "", ""
