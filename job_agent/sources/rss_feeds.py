from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List

import feedparser

from job_agent.models import Job
from job_agent.scoring import score_title
from job_agent.util import normalize_url, strip_html


def fetch_rss_jobs(feed_urls: List[str], cfg: Dict[str, Any]) -> List[Job]:
    out: List[Job] = []
    seen: set[str] = set()

    for url in feed_urls:
        if not url or not url.startswith("http"):
            continue
        parsed = feedparser.parse(url)
        for e in parsed.entries or []:
            link = getattr(e, "link", "") or ""
            if not link:
                continue
            title = getattr(e, "title", "") or "Job"
            link_n = normalize_url(link)
            if link_n in seen:
                continue
            seen.add(link_n)
            company = ""
            if " — " in title:
                parts = title.split(" — ", 1)
                title, company = parts[0].strip(), parts[1].strip()
            published_raw = getattr(e, "published", None) or getattr(e, "updated", None) or ""
            published_raw = str(published_raw).strip()
            posted = "recent"
            if published_raw:
                try:
                    dt = parsedate_to_datetime(published_raw)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    posted = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
                except (TypeError, ValueError):
                    posted = published_raw[:32]
            summary = getattr(e, "summary", None) or getattr(e, "description", None) or ""
            text_blob = strip_html(str(summary))
            out.append(
                Job(
                    source=f"rss:{url[:48]}",
                    company=company or (getattr(e, "author", "") or "Various"),
                    title=title,
                    location="Remote" if "remote" in url.lower() else "",
                    link=link_n,
                    posted=posted or "recent",
                    score=score_title(title, cfg),
                    raw={"text": text_blob[:12000]},
                )
            )
    return out
