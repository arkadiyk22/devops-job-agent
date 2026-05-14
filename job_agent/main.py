from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

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
    """Keep rows that look Israel-related per config (aliases + optional strict mode).

    Applies to sources listed in ``location_filter_source_prefixes`` (by default
    Greenhouse, Lever, RSS; config also includes ``serpapi_`` and ``google_site_ats:``
    for Israel-only across those feeds).

    SerpAPI Google Jobs is still scoped by ``serpapi_location`` / ``gl`` in
    ``google_jobs.py``; this filter is an additional text guard on returned rows.

    When ``location_hint_strict_location_or_title`` is true, a match must appear in
    **location or title** (not company alone, and not job description). That avoids
    US postings that only mention Israel in the body or company boilerplate.

    When ``location_hint_include_job_description`` is true and strict mode is off,
    the first ``location_hint_description_max_chars`` characters of ``Job.raw["text"]``
    are also searched.
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
    strict_loc_title = bool(cfg.get("location_hint_strict_location_or_title", False))
    include_desc = bool(cfg.get("location_hint_include_job_description", False)) and not strict_loc_title
    desc_cap = int(cfg.get("location_hint_description_max_chars") or 4000)
    if desc_cap < 500:
        desc_cap = 500
    if desc_cap > 50000:
        desc_cap = 50000

    def _has_alias(text: str) -> bool:
        low = text.lower()
        return any(a in low for a in aliases)

    for j in jobs:
        sl = j.source.lower()
        if not any(sl.startswith(p) for p in prefixes):
            out.append(j)
            continue
        loc = j.location or ""
        title = j.title or ""
        company = j.company or ""
        if strict_loc_title:
            if _has_alias(f"{loc} {title}"):
                out.append(j)
            continue
        if _has_alias(f"{loc} {title} {company}"):
            out.append(j)
            continue
        if include_desc and isinstance(j.raw, dict):
            txt = str(j.raw.get("text") or "")
            if txt and _has_alias(txt[:desc_cap]):
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


def collect_all_with_stats(cfg: Dict[str, Any], only: Set[str] | None) -> Tuple[List[Job], List[Dict[str, Any]]]:
    """Fetch from all enabled sources; return jobs and one stats row per site/feed queried."""
    jobs: List[Job] = []
    seen: Set[str] = set()
    stats: List[Dict[str, Any]] = []

    def add_many(new: List[Job], site_label: str) -> None:
        fetched = len(new)
        added_here = 0
        for j in new:
            if j.link not in seen:
                seen.add(j.link)
                jobs.append(j)
                added_here += 1
        stats.append({"Site": site_label, "Fetched": fetched, "Unique added": added_here})

    if only:
        only = _strip_disabled_serpapi_sources(cfg, only)
        if not only:
            only = None

    if serpapi_feature_enabled("google_jobs", cfg) and (only is None or "serpapi" in only or "google_jobs" in only):
        batch = fetch_google_jobs(build_serpapi_queries(cfg), cfg)
        add_many(batch, "SerpAPI: Google Jobs")

    if serpapi_feature_enabled("google_site_ats", cfg) and (
        only is None or "google_site_ats" in only or "ats_google" in only
    ):
        batch = fetch_google_site_ats(build_ats_google_site_queries(cfg), cfg)
        add_many(batch, "SerpAPI: Google web (site: ATS / LinkedIn)")

    if only is None or "greenhouse" in only:
        for board in cfg.get("greenhouse_boards") or []:
            b = str(board or "").strip()
            if not b:
                continue
            batch = fetch_greenhouse([b], cfg)
            add_many(batch, f"Greenhouse: {b}")

    if only is None or "lever" in only:
        for site in cfg.get("lever_sites") or []:
            s = str(site or "").strip()
            if not s:
                continue
            batch = fetch_lever([s], cfg)
            add_many(batch, f"Lever: {s}")

    if only is None or "rss" in only:
        for feed in cfg.get("rss_feeds") or []:
            u = str(feed or "").strip()
            if not u:
                continue
            batch = fetch_rss_jobs([u], cfg)
            label = u if len(u) <= 96 else u[:93] + "..."
            add_many(batch, f"RSS: {label}")

    return jobs, stats


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
    parser.add_argument(
        "--allow-non-israel-email",
        action="store_true",
        help="Disable digest_email_enforce_location_hint for this run (digest may include non-Israel rows).",
    )

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.allow_non_israel_email:
        cfg = {**cfg, "digest_email_enforce_location_hint": False}

    only = parse_sources_arg(args.sources or None)

    conn = db.connect(args.db)
    try:
        jobs, fetch_stats_rows = collect_all_with_stats(cfg, only)
        fetch_stats_df = pd.DataFrame(fetch_stats_rows)
        jobs = filter_jobs_by_location_hint(jobs, cfg)
        if not jobs:
            if not fetch_stats_df.empty:
                print("\nSources checked (raw fetch counts):", file=sys.stderr)
                print(fetch_stats_df.to_string(index=False), file=sys.stderr)
            if cfg.get("filter_jobs_by_location_hint", False):
                print(
                    "No jobs left after Israel / location_hint filter. "
                    "If location_hint_strict_location_or_title is true, an alias must appear "
                    "in the job title or location line (not company/description alone). "
                    "Global US boards often produce zero rows — add Israel-focused boards or SerpAPI IL queries.",
                    file=sys.stderr,
                )
            else:
                print("No jobs fetched from any source.")
            return 0
        jobs = apply_job_filters(jobs, cfg)
        annotate_search_fallback_for_blocked_domains(jobs, cfg)
        if not jobs:
            print("No jobs after age/closed posting filters.")
            if not fetch_stats_df.empty:
                print("\nSources checked (raw fetch counts):", file=sys.stderr)
                print(fetch_stats_df.to_string(index=False), file=sys.stderr)
            return 0

        existing = db.existing_links(conn)
        new_jobs = [j for j in jobs if j.link not in existing]
        if not new_jobs:
            print("No new jobs (all links already in jobs.db).")
            if not fetch_stats_df.empty:
                print("\nSources checked (raw fetch counts):", file=sys.stderr)
                print(fetch_stats_df.to_string(index=False), file=sys.stderr)
            return 0

        if cfg.get("digest_email_enforce_location_hint", True):
            gate_cfg = {**cfg, "filter_jobs_by_location_hint": True}
            before_gate = len(new_jobs)
            new_jobs = filter_jobs_by_location_hint(new_jobs, gate_cfg)
            dropped = before_gate - len(new_jobs)
            if dropped:
                print(
                    f"Digest email gate: removed {dropped} job(s) that do not match "
                    "Israel / location_hint rules (title+location strict match). "
                    "Use --allow-non-israel-email to send them anyway.",
                    file=sys.stderr,
                )
            if not new_jobs:
                print(
                    "Not sending digest: no jobs left after digest_email_enforce_location_hint.",
                    file=sys.stderr,
                )
                if not fetch_stats_df.empty:
                    print("\nSources checked (raw fetch counts):", file=sys.stderr)
                    print(fetch_stats_df.to_string(index=False), file=sys.stderr)
                return 0

        rows = [j.as_row() for j in new_jobs]
        df = pd.DataFrame(rows)
        by_src: Dict[str, int] = {}
        for j in new_jobs:
            by_src[j.source] = by_src.get(j.source, 0) + 1
        digest_by_source_df = pd.DataFrame(
            [{"Source": k, "New in this email": v} for k, v in sorted(by_src.items(), key=lambda kv: (-kv[1], kv[0]))]
        )

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
            if not fetch_stats_df.empty:
                print("\nSources checked (raw fetch counts):", file=sys.stderr)
                print(fetch_stats_df.to_string(index=False), file=sys.stderr)
            return 0

        db.insert_links(conn, [j.link for j in new_jobs])
        send_digest_email(
            df,
            contacts_for_out,
            cfg,
            network_df=network_df,
            fetch_stats_df=fetch_stats_df,
            digest_by_source_df=digest_by_source_df,
            attach_excel=attach_excel,
            excel_path=xlsx_path,
        )
        print("Sent digest email.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(run())
