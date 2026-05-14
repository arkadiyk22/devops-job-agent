from __future__ import annotations

from typing import Any, Dict, List

import requests

from job_agent.models import Job
from job_agent.scoring import score_title
from job_agent.util import normalize_url, strip_html


def fetch_greenhouse(boards: List[str], cfg: Dict[str, Any]) -> List[Job]:
    out: List[Job] = []
    seen: set[str] = set()

    for board in boards:
        board = (board or "").strip().strip("/")
        if not board:
            continue
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
        try:
            r = requests.get(url, timeout=25)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except ValueError:
            continue
        company = (data.get("name") or board).replace(" Job Board", "").strip()
        for job in data.get("jobs") or []:
            title = job.get("title", "") or ""
            if not title:
                continue
            tlow = title.lower()
            if not any(x in tlow for x in ("devops", "platform", "sre", "infrastructure", "infra", "engineering manager", "cloud")):
                continue
            if not any(x in tlow for x in ("manager", "director", "head", "lead", "vp", "vice")):
                continue
            link = job.get("absolute_url") or ""
            if not link:
                continue
            link_n = normalize_url(link)
            if link_n in seen:
                continue
            seen.add(link_n)
            loc = ""
            locs = job.get("location") or job.get("offices")
            if isinstance(locs, dict):
                loc = locs.get("name", "")
            elif isinstance(locs, list) and locs:
                loc = str(locs[0].get("name", "") if isinstance(locs[0], dict) else locs[0])
            updated = str(job.get("updated_at", "") or "").strip()
            posted = updated if updated else "recent"
            desc_text = strip_html(str(job.get("content", "") or ""))
            out.append(
                Job(
                    source=f"greenhouse:{board}",
                    company=company,
                    title=title,
                    location=loc,
                    link=link_n,
                    posted=posted,
                    score=score_title(title, cfg),
                    raw={"text": desc_text},
                )
            )
    return out
