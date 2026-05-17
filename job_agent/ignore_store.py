"""Persist jobs the user removed from digests (with optional snapshot for restore email)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

from job_agent.models import Job
from job_agent.util import normalize_url


def ignore_store_path(cfg: Dict[str, Any] | None = None) -> Path:
    block = (cfg or {}).get("digest_remove")
    if isinstance(block, dict):
        raw = (block.get("ignore_store_path") or "").strip()
        if raw:
            return Path(os.path.expanduser(raw))
    return Path.home() / ".job-agent" / "digest_ignore_links.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_raw_store(cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    path = ignore_store_path(cfg)
    if not path.is_file():
        return {"version": 2, "removed": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 2, "removed": []}
    if isinstance(data, list):
        return {
            "version": 2,
            "removed": [{"link": normalize_url(str(x).strip())} for x in data if str(x).strip()],
        }
    if not isinstance(data, dict):
        return {"version": 2, "removed": []}
    if isinstance(data.get("links"), list) and "removed" not in data:
        return {
            "version": 2,
            "removed": [{"link": normalize_url(str(x).strip())} for x in data["links"] if str(x).strip()],
        }
    if not isinstance(data.get("removed"), list):
        data["removed"] = []
    data.setdefault("version", 2)
    return data


def _save_raw_store(data: Dict[str, Any], cfg: Dict[str, Any] | None = None) -> Path:
    path = ignore_store_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 2, "removed": data.get("removed") if isinstance(data.get("removed"), list) else []}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def job_to_removed_record(job: Job, *, removed_at: str | None = None) -> Dict[str, Any]:
    raw = job.raw if isinstance(job.raw, dict) else {}
    reach = raw.get("reach_out_people") if isinstance(raw.get("reach_out_people"), list) else []
    return {
        "link": normalize_url(job.link),
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "source": job.source,
        "posted": job.posted,
        "score": job.score,
        "reach_out_people": reach,
        "removed_at": removed_at or _utc_now_iso(),
    }


def record_to_job(record: Dict[str, Any]) -> Job:
    raw: Dict[str, Any] = {}
    reach = record.get("reach_out_people")
    if isinstance(reach, list):
        raw["reach_out_people"] = reach
    return Job(
        source=str(record.get("source") or ""),
        company=str(record.get("company") or ""),
        title=str(record.get("title") or ""),
        location=str(record.get("location") or ""),
        link=str(record.get("link") or ""),
        posted=str(record.get("posted") or "recent"),
        score=int(record.get("score") or 0),
        raw=raw,
    )


def load_removed_records(cfg: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    data = _load_raw_store(cfg)
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in data.get("removed") or []:
        if not isinstance(item, dict):
            continue
        link = normalize_url(str(item.get("link") or "").strip())
        if not link or link in seen:
            continue
        seen.add(link)
        row = dict(item)
        row["link"] = link
        out.append(row)
    out.sort(key=lambda r: str(r.get("removed_at") or ""), reverse=True)
    return out


def load_stored_ignore_links(cfg: Dict[str, Any] | None = None) -> Set[str]:
    return {str(r["link"]) for r in load_removed_records(cfg) if r.get("link")}


def add_removed_record(record: Dict[str, Any], cfg: Dict[str, Any] | None = None) -> bool:
    """Add or update a removed job snapshot. Returns True if newly added."""
    link = normalize_url(str(record.get("link") or "").strip())
    if not link:
        return False
    data = _load_raw_store(cfg)
    removed: List[Dict[str, Any]] = [
        x for x in (data.get("removed") or []) if isinstance(x, dict) and normalize_url(str(x.get("link") or "")) != link
    ]
    is_new = not any(
        normalize_url(str(x.get("link") or "")) == link for x in (data.get("removed") or []) if isinstance(x, dict)
    )
    row = dict(record)
    row["link"] = link
    row.setdefault("removed_at", _utc_now_iso())
    removed.append(row)
    data["removed"] = removed
    _save_raw_store(data, cfg)
    return is_new


def add_ignore_link(link: str, cfg: Dict[str, Any] | None = None, *, job: Job | None = None) -> bool:
    if job is not None:
        return add_removed_record(job_to_removed_record(job), cfg)
    return add_removed_record({"link": normalize_url(link.strip())}, cfg)


def record_needs_detail(record: Dict[str, Any]) -> bool:
    """True when title or company is missing (common for legacy link-only removals)."""
    title = str(record.get("title") or "").strip()
    company = str(record.get("company") or "").strip()
    return not title or not company


def save_all_removed_records(records: List[Dict[str, Any]], cfg: Dict[str, Any] | None = None) -> Path:
    normalized: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            continue
        link = normalize_url(str(item.get("link") or "").strip())
        if not link or link in seen:
            continue
        seen.add(link)
        row = dict(item)
        row["link"] = link
        normalized.append(row)
    return _save_raw_store({"version": 2, "removed": normalized}, cfg)


def restore_removed_link(link: str, cfg: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
    """Remove from hide list and return the stored snapshot (if any)."""
    key = normalize_url(link.strip())
    if not key:
        return None
    data = _load_raw_store(cfg)
    kept: List[Dict[str, Any]] = []
    found: Dict[str, Any] | None = None
    for item in data.get("removed") or []:
        if not isinstance(item, dict):
            continue
        item_link = normalize_url(str(item.get("link") or ""))
        if item_link == key:
            found = dict(item)
        else:
            kept.append(item)
    if found is None:
        return None
    data["removed"] = kept
    _save_raw_store(data, cfg)
    return found


def removed_records_to_jobs(records: List[Dict[str, Any]]) -> List[Job]:
    jobs: List[Job] = []
    for rec in records:
        if rec.get("link"):
            jobs.append(record_to_job(rec))
    return jobs


def build_removed_jobs_dataframe(records: List[Dict[str, Any]]) -> "Any":
    """DataFrame rows for the removed-jobs digest email."""
    import re

    import pandas as pd

    from job_agent.network import format_reach_out_people

    rows: List[Dict[str, Any]] = []
    for rec in records:
        job = record_to_job(rec)
        row = job.as_row()
        if not str(row.get("Job Title") or "").strip():
            from job_agent.job_page_details import fallback_title_for_link

            row["Job Title"] = fallback_title_for_link(str(rec.get("link") or ""))
        if not str(row.get("Company") or "").strip():
            row["Company"] = "—"
        if not str(row.get("Location") or "").strip():
            row["Location"] = "—"
        reach = rec.get("reach_out_people")
        if isinstance(reach, list) and reach:
            people = [x for x in reach if isinstance(x, dict)]
            row["Network"] = format_reach_out_people(people) if people else ""
        else:
            row["Network"] = ""
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=["Job Title", "Company", "Network", "Link", "Source", "Location"])
    return pd.DataFrame(rows)


def merge_ignore_links(cfg: Dict[str, Any]) -> Set[str]:
    """Config digest_ignore_links plus user-marked removals from ~/.job-agent/."""
    raw_links = cfg.get("digest_ignore_links")
    out = {
        normalize_url(str(x).strip())
        for x in (raw_links if isinstance(raw_links, list) else [])
        if str(x).strip()
    }
    out |= load_stored_ignore_links(cfg)
    return out
