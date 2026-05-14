from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

import pandas as pd

from job_agent import db
from job_agent.contacts import find_contacts
from job_agent.excel_email import save_excel, send_digest_email
from job_agent.network import match_network_to_jobs, read_connections_csv, resolve_network_csv_path
from job_agent.filters import annotate_search_fallback_for_blocked_domains, apply_job_filters
from job_agent.models import Job
from job_agent.outreach import outreach_message
from job_agent.query_build import build_ats_google_site_queries, build_serpapi_queries
from job_agent.settings import get_setting, serpapi_feature_enabled
from job_agent.sources.google_jobs import fetch_google_jobs
from job_agent.sources.google_site_ats import fetch_google_site_ats
from job_agent.sources.greenhouse import fetch_greenhouse
from job_agent.sources.lever import fetch_lever
from job_agent.sources.rss_feeds import fetch_rss_jobs


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def filter_jobs_by_location_hint(jobs: List[Job], cfg: Dict[str, Any]) -> List[Job]:
    """For global boards (Greenhouse/Lever/RSS), keep rows that mention location_hint.

    SerpAPI Google Jobs rows are not filtered here — geography is applied via SerpAPI
    ``location`` / ``gl`` / ``google_domain`` in ``google_jobs.py``.
    """
    if not cfg.get("filter_jobs_by_location_hint", False):
        return jobs
    hint = (cfg.get("location_hint") or "").strip()
    if not hint:
        return jobs
    raw = cfg.get("location_filter_source_prefixes")
    if raw is None:
        prefixes = ("greenhouse:", "lever:", "rss:")
    elif isinstance(raw, list) and len(raw) == 0:
        return jobs
    else:
        prefixes = tuple(str(x).lower() for x in raw)
    needle = hint.lower()
    aliases = [needle]
    for a in cfg.get("location_hint_aliases") or []:
        t = str(a).strip().lower()
        if t and t not in aliases:
            aliases.append(t)
    out: List[Job] = []
    for j in jobs:
        sl = j.source.lower()
        if not any(sl.startswith(p) for p in prefixes):
            out.append(j)
            continue
        blob = f"{j.location} {j.title} {j.company}".lower()
        if any(a in blob for a in aliases):
            out.append(j)
    return out


def parse_sources_arg(raw: str | None) -> Set[str] | None:
    if not raw:
        return None
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _strip_disabled_serpapi_sources(cfg: Dict[str, Any], only: Set[str]) -> Set[str] | None:
    out = set(only)
    if not serpapi_feature_enabled("google_jobs", cfg):
        out -= {"serpapi", "google_jobs"}
    if not serpapi_feature_enabled("google_site_ats", cfg):
        out -= {"google_site_ats", "ats_google"}
    return out if out else None


def collect_all(cfg: Dict[str, Any], only: Set[str] | None) -> List[Job]:
    jobs: List[Job] = []
    seen: Set[str] = set()

    def add_many(new: List[Job]) -> None:
        for j in new:
            if j.link not in seen:
                seen.add(j.link)
                jobs.append(j)

    if only:
        only = _strip_disabled_serpapi_sources(cfg, only)
        if not only:
            only = None

    if serpapi_feature_enabled("google_jobs", cfg) and (only is None or "serpapi" in only or "google_jobs" in only):
        add_many(fetch_google_jobs(build_serpapi_queries(cfg), cfg))

    if serpapi_feature_enabled("google_site_ats", cfg) and (
        only is None or "google_site_ats" in only or "ats_google" in only
    ):
        add_many(fetch_google_site_ats(build_ats_google_site_queries(cfg), cfg))

    if only is None or "greenhouse" in only:
        add_many(fetch_greenhouse(cfg.get("greenhouse_boards") or [], cfg))

    if only is None or "lever" in only:
        add_many(fetch_lever(cfg.get("lever_sites") or [], cfg))

    if only is None or "rss" in only:
        add_many(fetch_rss_jobs(cfg.get("rss_feeds") or [], cfg))

    return jobs


def run(argv: List[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    root = project_root()
    parser = argparse.ArgumentParser(description="DevOps Manager/Director job agent")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("JOB_AGENT_CONFIG", str(root / "config.json"))),
        help="Path to config.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + build Excel; do not write jobs.db or send email",
    )
    parser.add_argument(
        "--skip-contacts",
        action="store_true",
        help="Skip SerpAPI Google search for LinkedIn profiles",
    )
    parser.add_argument(
        "--skip-network",
        action="store_true",
        help="Skip matching jobs to your LinkedIn Connections CSV (see config.network)",
    )
    parser.add_argument(
        "--sources",
        type=str,
        default="",
        help="Comma list: serpapi,google_site_ats,greenhouse,lever,rss (SerpAPI requires matching serpapi_features.* flags)",
    )
    parser.add_argument("--db", type=Path, default=root / "jobs.db", help="SQLite path")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    only = parse_sources_arg(args.sources or None)

    conn = db.connect(args.db)
    try:
        jobs = collect_all(cfg, only)
        jobs = filter_jobs_by_location_hint(jobs, cfg)
        if not jobs:
            print("No jobs fetched from any source.")
            return 0
        jobs = apply_job_filters(jobs, cfg)
        annotate_search_fallback_for_blocked_domains(jobs, cfg)
        if not jobs:
            print("No jobs after age/closed posting filters.")
            return 0

        existing = db.existing_links(conn)
        new_jobs = [j for j in jobs if j.link not in existing]
        if not new_jobs:
            print("No new jobs (all links already in jobs.db).")
            return 0

        rows = [j.as_row() for j in new_jobs]
        df = pd.DataFrame(rows)

        top_block = cfg.get("contact_search")
        top_block = top_block if isinstance(top_block, dict) else {}
        top_n = int(top_block.get("top_jobs_for_contacts") or 8)
        max_contacts_total = int(top_block.get("max_contacts_total") or 40)

        sorted_jobs = sorted(new_jobs, key=lambda j: (-j.score, j.title))[: max(1, top_n)]
        contacts: List[dict] = []
        if not args.skip_contacts:
            if serpapi_feature_enabled("contacts", cfg) and get_setting("SERPAPI_KEY", "GOOGLE_JOBS_API_KEY"):
                for j in sorted_jobs:
                    contacts.extend(find_contacts(j.company, j.title, j.link, cfg))
                contacts = contacts[: max_contacts_total]
            elif not serpapi_feature_enabled("contacts", cfg):
                print(
                    "Skipping contacts: SerpAPI contacts off (set serpapi_features.contacts or legacy use_serpapi).",
                    file=sys.stderr,
                )
            else:
                print("Skipping contacts: no SERPAPI_KEY", file=sys.stderr)

        contacts_df = pd.DataFrame(contacts)
        if not contacts_df.empty:
            contacts_df["Message"] = contacts_df.apply(
                lambda r: outreach_message(
                    str(r["Company"]),
                    str(r["Role Hint"]),
                    str(r.get("Job Link", "")),
                ),
                axis=1,
            )

        network_df = pd.DataFrame()
        net_path = resolve_network_csv_path(root, cfg)
        if not args.skip_network and net_path is not None:
            if net_path.is_file():
                conns = read_connections_csv(net_path)
                net_rows = match_network_to_jobs(new_jobs, conns)
                network_df = pd.DataFrame(net_rows)
                print(
                    f"Network: loaded {len(conns)} connections from {net_path.name}; "
                    f"{len(net_rows)} match rows for this digest."
                )
            else:
                print(f"Network: configured CSV not found: {net_path}", file=sys.stderr)

        out_dir = root if not args.dry_run else Path("/tmp")
        attach_excel = bool(cfg.get("email_attach_excel", False))
        contacts_for_out = contacts_df if not contacts_df.empty else pd.DataFrame()
        xlsx_path = None
        if attach_excel:
            xlsx_path = save_excel(df, contacts_for_out, out_dir, cfg, network_df=network_df)
            print(f"Wrote {xlsx_path} ({len(new_jobs)} new jobs).")
        else:
            print(f"Prepared digest for {len(new_jobs)} new jobs (HTML email body; no Excel).")

        if args.dry_run:
            print("Dry-run: not updating jobs.db or sending email.")
            return 0

        db.insert_links(conn, [j.link for j in new_jobs])
        send_digest_email(
            df,
            contacts_for_out,
            cfg,
            network_df=network_df,
            attach_excel=attach_excel,
            excel_path=xlsx_path,
        )
        print("Sent digest email.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(run())
