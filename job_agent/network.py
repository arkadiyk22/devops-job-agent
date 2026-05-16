"""Offline matching of job companies to people in *your* LinkedIn network.

LinkedIn does not offer a public API to answer "which of my connections work at
company X?". What people call **network** here is usually: use your own
relationships for warm intros. This module supports that by loading the
**Connections** CSV from a LinkedIn data export and matching the **Company**
column on each connection to the **company** string on each job (fuzzy match).

Export: LinkedIn → Settings & Privacy → Data privacy → Get a copy of your data →
pick **Connections** → Request archive → unzip and point ``config.json`` at
``Connections.csv``.
"""

from __future__ import annotations

import csv
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd

from job_agent.models import Job

_SUFFIX_RE = re.compile(
    r"\b(inc\.?|llc\.?|ltd\.?|limited|corp\.?|corporation|plc|gmbh|bv|s\.a\.|s\.p\.a\.)\.?\s*$",
    re.I,
)


def normalize_company(name: str) -> str:
    s = (name or "").strip().lower()
    s = _SUFFIX_RE.sub("", s).strip()
    s = re.sub(r"[^\w\s&]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def companies_match(job_company: str, connection_company: str, *, min_chars: int = 4) -> bool:
    """True if job posting company and connection's listed employer likely refer to the same org."""
    a = normalize_company(job_company)
    b = normalize_company(connection_company)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < min_chars or len(b) < min_chars:
        return False
    return a in b or b in a


def _cell_ci(row: Dict[str, str], *header_candidates: str) -> str:
    lower = {str(k).strip().lower(): (str(v) if v is not None else "").strip() for k, v in row.items()}
    for cand in header_candidates:
        lk = cand.lower()
        if lk in lower:
            return lower[lk]
    return ""


def read_connections_csv(path: Path) -> List[Dict[str, str]]:
    """Parse LinkedIn ``Connections.csv`` into normalized rows."""
    if not path.is_file():
        return []
    encodings: Tuple[str, ...] = ("utf-8-sig", "utf-16", "utf-8")
    last_err: Optional[Exception] = None
    for enc in encodings:
        try:
            raw_text = path.read_text(encoding=enc)
        except (UnicodeError, UnicodeDecodeError) as e:
            last_err = e
            continue
        lines = raw_text.splitlines()
        if not lines:
            return []
        reader = csv.DictReader(lines)
        if not reader.fieldnames:
            return []
        out: List[Dict[str, str]] = []
        for raw in reader:
            row = {str(k): str(v) if v is not None else "" for k, v in raw.items()}
            first = _cell_ci(row, "First Name", "FirstName", "Given Name")
            last = _cell_ci(row, "Last Name", "LastName", "Family Name")
            url = _cell_ci(row, "URL", "Profile URL", "LinkedIn URL")
            company = _cell_ci(row, "Company", "Organization", "Company Name")
            position = _cell_ci(row, "Position", "Title", "Headline", "Job Title")
            if not company or not url:
                continue
            low = url.lower()
            if "linkedin.com/in/" not in low and "lnkd.in/" not in low:
                continue
            name = f"{first} {last}".strip() or url
            out.append(
                {
                    "first_name": first,
                    "last_name": last,
                    "name": name,
                    "profile_url": url,
                    "connection_company": company,
                    "position": position,
                }
            )
        return out
    if last_err:
        print(f"network: could not decode {path}: {last_err}", file=sys.stderr)
    return []


def connections_for_job(job: Job, connections: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Connections whose export employer matches this job's company."""
    jc = (job.company or "").strip()
    if len(jc) < 2 or jc.lower() in ("unknown", "various", "n/a"):
        return []
    out: List[Dict[str, str]] = []
    seen_urls: Set[str] = set()
    for c in connections:
        if not companies_match(jc, c["connection_company"]):
            continue
        url = c["profile_url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        out.append(c)
    return out


def format_reach_out_person(p: Dict[str, str]) -> str:
    """Format one LinkedIn «People you can reach out to» row."""
    name = (p.get("name") or "").strip()
    role = (p.get("role") or "").strip()
    if name and role:
        return f"{name} ({role})"
    return name or role


def format_reach_out_people(people: List[Dict[str, str]], *, max_people: int = 8) -> str:
    if not people:
        return ""
    cap = max(1, max_people)
    parts = [format_reach_out_person(p) for p in people[:cap]]
    parts = [p for p in parts if p]
    extra = len(people) - cap
    text = "; ".join(parts)
    if extra > 0:
        text = f"{text}; +{extra} more" if text else f"+{extra} more"
    return text


def network_column_for_job(
    job: Job,
    connections: List[Dict[str, str]],
    cfg: Dict[str, Any],
) -> str:
    """Prefer LinkedIn «reach out» people from browser scrape, else Connections.csv."""
    block = cfg.get("network") if isinstance(cfg.get("network"), dict) else {}
    max_people = int(block.get("max_connections_per_job_in_column") or 8)
    raw = job.raw if isinstance(job.raw, dict) else {}
    scraped = raw.get("reach_out_people")
    if isinstance(scraped, list) and scraped:
        rows = [x for x in scraped if isinstance(x, dict)]
        if rows:
            return format_reach_out_people(rows, max_people=max_people)
    return network_column_text(job, connections, max_people=max_people)


def format_connection_short(c: Dict[str, str]) -> str:
    """One line for the Network column: ``Name (Role)``."""
    name = (c.get("name") or "").strip()
    role = (c.get("position") or "").strip()
    if name and role:
        return f"{name} ({role})"
    return name or role or c.get("profile_url", "")


def network_column_text(
    job: Job,
    connections: List[Dict[str, str]],
    *,
    max_people: int = 8,
) -> str:
    """Semicolon-separated list of 1st-degree connections at the job company."""
    matches = connections_for_job(job, connections)
    if not matches:
        return ""
    cap = max(1, max_people)
    parts = [format_connection_short(c) for c in matches[:cap]]
    extra = len(matches) - cap
    text = "; ".join(p for p in parts if p)
    if extra > 0:
        text = f"{text}; +{extra} more" if text else f"+{extra} more"
    return text


def enrich_jobs_dataframe_with_network(
    df: pd.DataFrame,
    jobs: Iterable[Job],
    connections: List[Dict[str, str]],
    cfg: Dict[str, Any],
) -> pd.DataFrame:
    """Add or fill ``Network`` column on the jobs digest table."""
    block = cfg.get("network") if isinstance(cfg.get("network"), dict) else {}
    max_people = int(block.get("max_connections_per_job_in_column") or 8)
    by_link = {j.link: network_column_for_job(j, connections, cfg) for j in jobs}
    out = df.copy()
    out["Network"] = out["Link"].map(lambda link: by_link.get(str(link), "") if link in by_link else "")
    if "Link" not in out.columns:
        out["Network"] = [network_column_for_job(j, connections, cfg) for j in jobs]
    return out


def match_network_to_jobs(jobs: Iterable[Job], connections: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """For each job, list connections whose export ``Company`` matches the job's company."""
    rows: List[Dict[str, Any]] = []
    if not connections:
        return rows
    seen: Set[Tuple[str, str]] = set()
    for job in jobs:
        jc = (job.company or "").strip()
        if len(jc) < 2 or jc.lower() in ("unknown", "various", "n/a"):
            continue
        for c in connections:
            if not companies_match(jc, c["connection_company"]):
                continue
            key = (job.link, c["profile_url"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "Job Title": job.title,
                    "Company": job.company,
                    "Job Link": job.link,
                    "Connection": c["name"],
                    "Network relation": (
                        "Your LinkedIn 1st-degree connection (from Connections export). "
                        f"Employer in export «{c['connection_company']}» matches the job posting company «{jc}»."
                    ),
                    "Their company (export)": c["connection_company"],
                    "Their role": c["position"],
                    "Profile": c["profile_url"],
                }
            )
    return rows


def _expand_csv_path(raw: str, root: Path) -> Path:
    p = Path(os.path.expanduser(raw.strip()))
    if not p.is_absolute():
        p = (root / p).resolve()
    return p


def resolve_network_csv_path(root: Path, cfg: Dict[str, Any]) -> Optional[Path]:
    """Config path, env ``LINKEDIN_CONNECTIONS_CSV``, then common export locations."""
    block = cfg.get("network")
    block = block if isinstance(block, dict) else {}
    raw = (block.get("linkedin_connections_csv") or "").strip()
    if raw:
        return _expand_csv_path(raw, root)

    env = (os.getenv("LINKEDIN_CONNECTIONS_CSV") or "").strip()
    if env:
        p = _expand_csv_path(env, root)
        return p

    candidates: List[Path] = [
        root / "Connections.csv",
        Path.home() / "Downloads" / "Connections.csv",
        Path.home() / "Documents" / "Connections.csv",
        Path.home() / "Desktop" / "Connections.csv",
    ]
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        for sub in sorted(downloads.glob("**/Connections.csv"))[:20]:
            candidates.append(sub)

    seen: Set[str] = set()
    for p in candidates:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        if p.is_file():
            return p.resolve()
    return None
