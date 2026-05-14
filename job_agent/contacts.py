from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from job_agent.serpapi_client import serpapi_request
from job_agent.settings import get_setting, serpapi_feature_enabled


def _serpapi_key_ok() -> bool:
    k = (get_setting("SERPAPI_KEY", "GOOGLE_JOBS_API_KEY") or "").strip()
    if not k or k.lower().startswith("your_") or k.lower() in ("changeme", "xxx", "placeholder"):
        return False
    return True


def _is_linkedin_profile_url(link: str) -> bool:
    low = (link or "").lower()
    return "linkedin.com/in/" in low or "lnkd.in/" in low


def _build_contact_queries(company: str, job_title: str, cfg: Optional[Dict[str, Any]]) -> List[str]:
    c = (company or "").strip()
    t = (job_title or "").strip()
    block = (cfg or {}).get("contact_search") if isinstance(cfg, dict) else None
    block = block if isinstance(block, dict) else {}

    extra = block.get("google_queries_extra")
    custom: List[str] = []
    if isinstance(extra, list):
        for raw in extra:
            s = str(raw).strip()
            if not s:
                continue
            custom.append(
                s.replace("{company}", c)
                .replace("{job_title}", t)
                .replace("{role}", t)
            )

    core = [
        f'"{c}" "{t}" site:linkedin.com/in',
        f'"{c}" {t} site:linkedin.com/in',
        f"{c} DevOps hiring manager site:linkedin.com/in",
        f"{c} technical recruiter site:linkedin.com/in",
        f"{c} talent acquisition site:linkedin.com/in",
        f"{c} engineering manager infrastructure site:linkedin.com/in",
        f"{c} head of engineering site:linkedin.com/in",
        f"{c} recruiter site:linkedin.com/in",
        f"{c} human resources site:linkedin.com/in",
    ]
    out: List[str] = []
    seen_q: set[str] = set()
    for q in custom + core:
        q = " ".join(q.split()).strip()
        if q and q not in seen_q:
            seen_q.add(q)
            out.append(q)
    return out


def _google_params(q: str, api_key: str, cfg: Optional[Dict[str, Any]]) -> Dict[str, str]:
    p: Dict[str, str] = {"engine": "google", "q": q, "api_key": api_key}
    if isinstance(cfg, dict):
        gd = (cfg.get("serpapi_google_domain") or "").strip()
        if gd:
            p["google_domain"] = gd
        gl = (cfg.get("serpapi_gl") or "").strip()
        if gl:
            p["gl"] = gl
        hl = (cfg.get("serpapi_hl") or "").strip()
        if hl:
            p["hl"] = hl
        num = (cfg.get("contact_search") or {}).get("google_num") if isinstance(cfg.get("contact_search"), dict) else None
        if isinstance(num, int) and num > 0:
            p["num"] = str(min(num, 20))
    return p


def _google_search_once(params: Dict[str, str]) -> Dict[str, Any]:
    return serpapi_request(params)


def find_contacts(
    company: str,
    job_title: str,
    job_link: str = "",
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Use Google (SerpAPI) to find LinkedIn profiles likely tied to hiring for this role."""
    company = (company or "").strip()
    job_title = (job_title or "").strip()
    if len(company) < 2 or company.lower() in ("unknown", "various", "n/a"):
        return []
    if not serpapi_feature_enabled("contacts", cfg or {}):
        return []
    if not _serpapi_key_ok():
        return []

    api_key = get_setting("SERPAPI_KEY", "GOOGLE_JOBS_API_KEY")
    block = (cfg or {}).get("contact_search") if isinstance(cfg, dict) else None
    block = block if isinstance(block, dict) else {}
    max_per_job = int(block.get("max_contacts_per_job") or 5)
    organic_cap = int(block.get("organic_results_per_query") or 3)

    contacts: List[Dict[str, Any]] = []
    for q in _build_contact_queries(company, job_title, cfg):
        try:
            data = _google_search_once(_google_params(q, api_key, cfg))
        except RuntimeError as e:
            msg = str(e)
            if "429" in msg or "run out of searches" in msg.lower():
                print("SerpAPI (contacts): quota exhausted — stopping contact lookup.", file=sys.stderr)
                break
            raise

        for r in (data.get("organic_results") or [])[:organic_cap]:
            if not isinstance(r, dict):
                continue
            link = (r.get("link") or "").strip()
            if not _is_linkedin_profile_url(link):
                continue
            contacts.append(
                {
                    "Company": company,
                    "Role Hint": job_title,
                    "Job Link": job_link,
                    "Name/Title": r.get("title", ""),
                    "Profile": link,
                    "LinkedIn Profile": link,
                    "Snippet": r.get("snippet", ""),
                    "Search query": q,
                }
            )

    seen: set[str] = set()
    uniq: List[Dict[str, Any]] = []
    for c in contacts:
        prof = c.get("LinkedIn Profile") or c.get("Profile") or ""
        if prof and prof not in seen:
            uniq.append(c)
            seen.add(prof)
        if len(uniq) >= max_per_job:
            break
    return uniq
