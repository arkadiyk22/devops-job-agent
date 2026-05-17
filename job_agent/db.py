from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from job_agent.models import Job


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path | str = "jobs.db") -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE IF NOT EXISTS jobs (link TEXT PRIMARY KEY)")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "payload" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN payload TEXT")
    if "first_seen_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN first_seen_at TEXT")
    if "emailed_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN emailed_at TEXT")
    if "last_seen_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN last_seen_at TEXT")
    # Legacy link-only rows: treat as already emailed so they are not bulk-sent once.
    conn.execute(
        "UPDATE jobs SET emailed_at = COALESCE(emailed_at, ?) WHERE payload IS NULL",
        (_utc_now_iso(),),
    )
    conn.commit()


def existing_links(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT link FROM jobs")}


def insert_links(conn: sqlite3.Connection, links: list[str]) -> None:
    """Backward-compatible link-only insert (marks as emailed)."""
    now = _utc_now_iso()
    for link in links:
        if link:
            conn.execute(
                "INSERT OR IGNORE INTO jobs (link, first_seen_at, emailed_at) VALUES (?, ?, ?)",
                (link, now, now),
            )
    conn.commit()


def job_to_payload(job: Job) -> str:
    raw = job.raw if isinstance(job.raw, dict) else {}
    return json.dumps(
        {
            "source": job.source,
            "company": job.company,
            "title": job.title,
            "location": job.location,
            "link": job.link,
            "posted": job.posted,
            "score": job.score,
            "search_fallback": job.search_fallback,
            "raw": raw,
        },
        ensure_ascii=False,
    )


def job_from_payload(payload: str) -> Job | None:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("link"):
        return None
    return Job(
        source=str(data.get("source") or ""),
        company=str(data.get("company") or ""),
        title=str(data.get("title") or ""),
        location=str(data.get("location") or ""),
        link=str(data.get("link") or ""),
        posted=str(data.get("posted") or "recent"),
        score=int(data.get("score") or 0),
        search_fallback=str(data.get("search_fallback") or ""),
        raw=data.get("raw") if isinstance(data.get("raw"), dict) else {},
    )


def upsert_jobs(conn: sqlite3.Connection, jobs: List[Job], *, mark_emailed: bool) -> int:
    """Store job payloads. Returns count of newly inserted links."""
    now = _utc_now_iso()
    new_count = 0
    for job in jobs:
        if not job.link:
            continue
        payload = job_to_payload(job)
        emailed_at = now if mark_emailed else None
        cur = conn.execute("SELECT link FROM jobs WHERE link = ?", (job.link,))
        exists = cur.fetchone() is not None
        if exists:
            conn.execute(
                "UPDATE jobs SET payload = ?, last_seen_at = ?, emailed_at = COALESCE(emailed_at, ?) WHERE link = ?",
                (payload, now, emailed_at, job.link),
            )
        else:
            conn.execute(
                "INSERT INTO jobs (link, first_seen_at, last_seen_at, emailed_at, payload) VALUES (?, ?, ?, ?, ?)",
                (job.link, now, now, emailed_at, payload),
            )
            new_count += 1
    conn.commit()
    return new_count


def load_job_by_link(conn: sqlite3.Connection, link: str) -> Job | None:
    key = (link or "").strip()
    if not key:
        return None
    row = conn.execute("SELECT payload FROM jobs WHERE link = ?", (key,)).fetchone()
    if not row or not row[0]:
        return None
    return job_from_payload(str(row[0]))


def load_pending_jobs(conn: sqlite3.Connection) -> List[Job]:
    """Jobs stored but not yet included in a digest email."""
    out: List[Job] = []
    for (payload,) in conn.execute("SELECT payload FROM jobs WHERE emailed_at IS NULL AND payload IS NOT NULL"):
        job = job_from_payload(str(payload or ""))
        if job:
            out.append(job)
    return out


def load_all_stored_jobs(conn: sqlite3.Connection) -> List[Job]:
    """All jobs with stored payloads (for repeat digests that include the same listings each time)."""
    out: List[Job] = []
    for (payload,) in conn.execute(
        "SELECT payload FROM jobs WHERE payload IS NOT NULL ORDER BY COALESCE(last_seen_at, first_seen_at) DESC"
    ):
        job = job_from_payload(str(payload or ""))
        if job:
            out.append(job)
    return out


def load_recent_stored_jobs(conn: sqlite3.Connection, *, within_days: float) -> List[Job]:
    """Jobs seen in a recent fetch (avoids stale listings from old DB rows in each digest)."""
    days = max(0.25, float(within_days or 2))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    out: List[Job] = []
    for (payload,) in conn.execute(
        """
        SELECT payload FROM jobs
        WHERE payload IS NOT NULL
          AND COALESCE(last_seen_at, first_seen_at) >= ?
        ORDER BY COALESCE(last_seen_at, first_seen_at) DESC
        """,
        (cutoff,),
    ):
        job = job_from_payload(str(payload or ""))
        if job:
            out.append(job)
    return out


def mark_emailed(conn: sqlite3.Connection, links: List[str]) -> None:
    now = _utc_now_iso()
    for link in links:
        if link:
            conn.execute("UPDATE jobs SET emailed_at = ? WHERE link = ?", (now, link))
    conn.commit()


def delete_jobs(conn: sqlite3.Connection, links: List[str]) -> int:
    """Remove job rows (used when user marks Remove → Yes in digest email)."""
    deleted = 0
    for link in links:
        if not link:
            continue
        cur = conn.execute("DELETE FROM jobs WHERE link = ?", (link,))
        deleted += cur.rowcount
    conn.commit()
    return deleted


def filter_new_links(conn: sqlite3.Connection, links: list[str]) -> list[str]:
    have = existing_links(conn)
    return [ln for ln in links if ln and ln not in have]
