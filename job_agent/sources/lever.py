from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

from job_agent.models import Job
from job_agent.scoring import score_title
from job_agent.util import normalize_url


def _title_from_lever_text(text: str) -> str:
    if not text:
        return "Role"
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:200]
    return "Role"


def fetch_lever(sites: List[str], cfg: Dict[str, Any]) -> List[Job]:
    out: List[Job] = []
    seen: set[str] = set()

    for site in sites:
        site = (site or "").strip().strip("/")
        if not site:
            continue
        url = f"https://api.lever.co/v0/postings/{site}?mode=json"
        try:
            r = requests.get(url, timeout=25)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        try:
            postings = r.json()
        except ValueError:
            continue
        if not isinstance(postings, list):
            continue
        for p in postings:
            text = p.get("text", "") or ""
            title = _title_from_lever_text(text)
            tlow = title.lower()
            if not any(x in tlow for x in ("devops", "platform", "sre", "infrastructure", "infra", "engineering", "cloud")):
                continue
            if not any(x in tlow for x in ("manager", "director", "head", "lead", "vp")):
                continue
            link = p.get("hostedUrl") or p.get("applyUrl") or ""
            if not link:
                continue
            link_n = normalize_url(link)
            if link_n in seen:
                continue
            seen.add(link_n)
            cats = p.get("categories") or {}
            team = cats.get("team", "") if isinstance(cats, dict) else ""
            loc_raw = cats.get("location", "") if isinstance(cats, dict) else ""
            if isinstance(loc_raw, list):
                location = ", ".join(str(x) for x in loc_raw)
            else:
                location = str(loc_raw or "")
            company = site.replace("-", " ").title()
            created = p.get("createdAt")
            posted = "recent"
            if isinstance(created, (int, float)) and created > 0:
                ts = float(created) / 1000.0 if created > 1e12 else float(created)
                posted = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            elif isinstance(created, str) and created.strip():
                posted = created.strip()[:19]
            out.append(
                Job(
                    source=f"lever:{site}",
                    company=company + (f" ({team})" if team else ""),
                    title=title,
                    location=location,
                    link=link_n,
                    posted=posted,
                    score=score_title(title, cfg),
                    raw={"text": (text or "")[:12000]},
                )
            )
    return out
