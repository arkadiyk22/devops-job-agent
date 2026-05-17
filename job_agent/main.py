from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pandas as pd

from job_agent import db
from job_agent.digest_guard import record_send, should_skip_send
from job_agent.contacts import _build_contact_queries, find_contacts
from job_agent.digest_remove import (
    digest_remove_enabled,
    ensure_remove_server_running,
    run_remove_server_forever,
)
from job_agent.excel_email import save_excel, send_digest_email
from job_agent.job_tracker_excel import (
    allowed_status_values,
    apply_tracker_to_digest_df,
    create_empty_job_tracker,
    default_job_tracker_path,
    job_tracker_enabled,
    sync_digest_jobs_to_tracker,
    update_job_status,
)
from job_agent.network import (
    connections_for_job,
    enrich_jobs_dataframe_with_network,
    match_network_to_jobs,
    normalize_company,
    read_connections_csv,
    resolve_network_csv_path,
)
from job_agent.ignore_store import (
    build_removed_jobs_dataframe,
    load_removed_records,
    merge_ignore_links,
    removed_records_to_jobs,
)
from job_agent.util import normalize_url
from job_agent.filters import annotate_search_fallback_for_blocked_domains, apply_job_filters
from job_agent.location_filter import filter_jobs_by_location_hint
from job_agent.models import Job
from job_agent.outreach import outreach_message
from job_agent.query_build import build_ats_google_site_queries, build_google_browser_queries, build_serpapi_queries
from job_agent.search_mode import uses_browser_search
from job_agent.search.v2.planner import build_search_plan, describe_plan_text
from job_agent.settings import get_setting, serpapi_feature_enabled
from job_agent.sources.google_jobs import fetch_google_jobs
from job_agent.sources.google_site_ats import fetch_google_site_ats
from job_agent.browser.paths import resolve_browser_user_data_dir
from job_agent.sources.google_browser import fetch_google_web_browser, google_login
from job_agent.sources.linkedin_browser import (
    build_linkedin_jobs_search_url,
    enrich_removed_records,
    enrich_reach_out_for_jobs,
    fetch_linkedin_jobs,
    linkedin_login,
)
from job_agent.sources.greenhouse import fetch_greenhouse
from job_agent.sources.lever import fetch_lever
from job_agent.sources.rss_feeds import fetch_rss_jobs


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_sources_arg(raw: str | None) -> Set[str] | None:
    if not raw:
        return None
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _digest_subject(cfg: Dict[str, Any], slot: str) -> str | None:
    custom = cfg.get("digest_email_subjects")
    if isinstance(custom, dict) and slot in custom:
        t = str(custom[slot] or "").strip()
        if t:
            return t
    if slot == "morning":
        return "DevOps leadership jobs — morning digest (Israel)"
    if slot == "afternoon":
        return "DevOps leadership jobs — afternoon digest (Israel)"
    if slot == "digest":
        return "DevOps leadership jobs — digest (Israel)"
    if slot == "removed":
        return "DevOps leadership jobs — removed (restore)"
    return None


def _collect_and_filter_jobs(
    run_cfg: Dict[str, Any],
    cfg: Dict[str, Any],
    only: Set[str] | None,
) -> Tuple[List[Job], pd.DataFrame, int, int]:
    jobs, fetch_stats_rows = collect_all_with_stats(run_cfg, only)
    fetch_stats_df = pd.DataFrame(fetch_stats_rows)
    raw_job_count = len(jobs)
    jobs = filter_jobs_by_location_hint(jobs, cfg)
    after_location_count = len(jobs)
    if jobs:
        jobs = apply_job_filters(jobs, cfg)
        annotate_search_fallback_for_blocked_domains(jobs, cfg)
    jobs = _apply_digest_ignore(jobs, cfg)
    location_dropped = max(0, raw_job_count - after_location_count)
    return jobs, fetch_stats_df, raw_job_count, location_dropped


def _cfg_disable_reach_out_scrape(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Poll runs (--fetch-only) skip slow per-job LinkedIn reach-out scraping."""
    li = cfg.get("linkedin")
    if not isinstance(li, dict):
        return cfg
    js = li.get("jobs_search")
    if not isinstance(js, dict):
        return cfg
    return {**cfg, "linkedin": {**li, "jobs_search": {**js, "scrape_reach_out_people": False}}}


def _digest_only_new(cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("digest_email_only_new", False))


def _apply_digest_ignore(jobs: List[Job], cfg: Dict[str, Any]) -> List[Job]:
    ignore_links = merge_ignore_links(cfg)
    raw_cos = cfg.get("digest_ignore_companies")
    ignore_cos = {
        normalize_company(str(x))
        for x in (raw_cos if isinstance(raw_cos, list) else [])
        if str(x).strip()
    }
    if not ignore_links and not ignore_cos:
        return jobs
    out: List[Job] = []
    for j in jobs:
        if normalize_url(j.link) in ignore_links:
            continue
        if ignore_cos and normalize_company(j.company) in ignore_cos:
            continue
        out.append(j)
    return out


def _dedupe_jobs_by_link(jobs: List[Job]) -> List[Job]:
    """One row per job URL; keep highest score, then title."""
    best: Dict[str, Job] = {}
    for j in jobs:
        key = normalize_url(j.link)
        if not key:
            continue
        prev = best.get(key)
        if prev is None or (j.score, j.title) > (prev.score, prev.title):
            best[key] = j
    return sorted(best.values(), key=lambda x: (-x.score, x.title))


def _finalize_jobs_for_digest(jobs: List[Job], cfg: Dict[str, Any]) -> List[Job]:
    jobs = _dedupe_jobs_by_link(jobs)
    if cfg.get("digest_email_enforce_location_hint", True):
        jobs = filter_jobs_by_location_hint(jobs, cfg)
    jobs = apply_job_filters(jobs, cfg)
    jobs = _dedupe_jobs_by_link(jobs)
    return _apply_digest_ignore(jobs, cfg)


def _jobs_for_scheduled_digest(conn: Any, cfg: Dict[str, Any]) -> List[Job]:
    if _digest_only_new(cfg):
        jobs = db.load_pending_jobs(conn)
    else:
        within = float(cfg.get("digest_include_jobs_seen_within_days") or 2)
        jobs = db.load_recent_stored_jobs(conn, within_days=within)
    return _finalize_jobs_for_digest(jobs, cfg)


def _send_email_for_jobs(
    *,
    email_jobs: List[Job],
    cfg: Dict[str, Any],
    root: Path,
    args: argparse.Namespace,
    conn: Any,
    fetch_stats_df: pd.DataFrame,
    digest_note: str,
    subject: str | None,
    table_action: str = "remove",
    jobs_df: pd.DataFrame | None = None,
) -> int:
    if not email_jobs:
        print("No jobs to email.")
        return 0

    slot = (getattr(args, "digest_slot", None) or "").strip()
    if table_action == "restore":
        slot = slot or "removed"
    if not args.dry_run:
        skip, reason = should_skip_send(cfg, slot=slot)
        if skip:
            print(reason)
            return 0

    if table_action != "restore" and not args.dry_run and not args.skip_network and uses_browser_search(cfg):
        if conn is not None:
            from job_agent.network import linkedin_reach_out_snapshot_ok

            for j in email_jobs:
                if j.source != "linkedin_browser":
                    continue
                if linkedin_reach_out_snapshot_ok(j.raw if isinstance(j.raw, dict) else {}):
                    continue
                stored = db.load_job_by_link(conn, j.link)
                if not stored or not isinstance(stored.raw, dict):
                    continue
                if not linkedin_reach_out_snapshot_ok(stored.raw):
                    continue
                merged = dict(j.raw) if isinstance(j.raw, dict) else {}
                for key in ("reach_out_people", "reach_out_source", "reach_out_summary"):
                    if key in stored.raw:
                        merged[key] = stored.raw[key]
                j.raw = merged
        enrich_reach_out_for_jobs(email_jobs, cfg, for_email=True)
        if conn is not None:
            db.upsert_jobs(conn, email_jobs, mark_emailed=False)

    if jobs_df is not None and not jobs_df.empty:
        df = jobs_df.copy()
    else:
        rows = [j.as_row() for j in email_jobs]
        df = pd.DataFrame(rows)
    by_src: Dict[str, int] = {}
    for j in email_jobs:
        by_src[j.source] = by_src.get(j.source, 0) + 1
    digest_by_source_df = pd.DataFrame(
        [{"Source": k, "New in this email": v} for k, v in sorted(by_src.items(), key=lambda kv: (-kv[1], kv[0]))]
    )

    top_block = cfg.get("contact_search")
    top_block = top_block if isinstance(top_block, dict) else {}
    top_n = int(top_block.get("top_jobs_for_contacts") or 8)
    max_contacts_total = int(top_block.get("max_contacts_total") or 40)

    sorted_jobs = sorted(email_jobs, key=lambda j: (-j.score, j.title))[: max(1, top_n)]
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
    conns: List[Dict[str, str]] = []
    net_path = resolve_network_csv_path(root, cfg)
    if not args.skip_network:
        if net_path is not None and net_path.is_file():
            conns = read_connections_csv(net_path)
            net_rows = match_network_to_jobs(email_jobs, conns)
            network_df = pd.DataFrame(net_rows)
        df = enrich_jobs_dataframe_with_network(df, email_jobs, conns, cfg)
    else:
        df["Network"] = ""

    if table_action != "restore" and job_tracker_enabled(cfg):
        sync_digest_jobs_to_tracker(df, cfg, root=root)
        df = apply_tracker_to_digest_df(df, cfg, root=root)

    removed_df = pd.DataFrame()
    if table_action != "restore" and digest_remove_enabled(cfg) and cfg.get(
        "digest_email_include_removed_table", True
    ):
        records = load_removed_records(cfg)
        if records:
            records = enrich_removed_records(records, cfg)
            removed_df = build_removed_jobs_dataframe(records)
            print(f"Including {len(removed_df)} removed job(s) after main table.", file=sys.stderr)

    attach_excel = bool(cfg.get("email_attach_excel", False))
    contacts_for_out = contacts_df if not contacts_df.empty else pd.DataFrame()
    xlsx_path = None
    if attach_excel and not args.dry_run:
        xlsx_path = save_excel(df, contacts_for_out, root, cfg, network_df=network_df)

    if args.dry_run:
        print(f"Dry-run: would email {len(email_jobs)} job(s).")
        return 0

    if digest_remove_enabled(cfg) and table_action == "remove":
        if ensure_remove_server_running(cfg):
            print(
                "Digest action links enabled (Remove / Did U apply? → Yes).",
                file=sys.stderr,
            )
        else:
            print(
                "Warning: digest server is not running — Remove / Apply links will not work. "
                "Run: python3 run.py --digest-remove-server",
                file=sys.stderr,
            )

    if table_action != "restore":
        db.mark_emailed(conn, [j.link for j in email_jobs])
    record_send(cfg, slot=slot or table_action, job_count=len(email_jobs))
    send_digest_email(
        df,
        contacts_for_out,
        cfg,
        network_df=network_df,
        fetch_stats_df=fetch_stats_df,
        digest_by_source_df=digest_by_source_df,
        digest_note=digest_note,
        attach_excel=attach_excel,
        excel_path=xlsx_path,
        subject=subject,
        table_action=table_action if table_action in ("remove", "restore") else "remove",
        removed_jobs_df=removed_df,
    )
    print(f"Sent digest email ({len(email_jobs)} jobs).")
    return 0


def _send_removed_jobs_email(
    *,
    cfg: Dict[str, Any],
    root: Path,
    args: argparse.Namespace,
    conn: Any,
) -> int:
    records = load_removed_records(cfg)
    if not records:
        print("No removed jobs in your hide list.")
        return 0
    records = enrich_removed_records(records, cfg)
    email_jobs = removed_records_to_jobs(records)
    df = build_removed_jobs_dataframe(records)
    note = f"{len(records)} hidden job(s). Click Restore in a row to bring it back."
    if digest_remove_enabled(cfg):
        if ensure_remove_server_running(cfg):
            print("Restore links enabled in removed-jobs email.", file=sys.stderr)
        else:
            print(
                "Warning: remove/restore server not running — Restore links will not work. "
                "Run: python3 run.py --digest-remove-server",
                file=sys.stderr,
            )
    subject = _digest_subject(cfg, "removed")
    return _send_email_for_jobs(
        email_jobs=email_jobs,
        cfg=cfg,
        root=root,
        args=args,
        conn=conn,
        fetch_stats_df=pd.DataFrame(),
        digest_note=note,
        subject=subject,
        table_action="restore",
        jobs_df=df,
    )


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
        if not uses_browser_search(cfg):
            only = _strip_disabled_serpapi_sources(cfg, only)
        if not only:
            only = None

    if uses_browser_search(cfg):
        li = cfg.get("linkedin")
        li_on = isinstance(li, dict) and li.get("enabled", True)
        if li_on and (only is None or "linkedin" in only or "linkedin_browser" in only):
            try:
                batch = fetch_linkedin_jobs(cfg)
            except Exception as exc:
                print(f"LinkedIn (browser): failed ({exc})", file=sys.stderr)
                batch = []
            add_many(batch, "LinkedIn (browser)")
        gw = cfg.get("google_web_browser")
        gw_on = isinstance(gw, dict) and gw.get("enabled", True)
        if gw_on and (only is None or "google" in only or "google_browser" in only or "google_web" in only):
            try:
                batch = fetch_google_web_browser(cfg)
            except Exception as exc:
                print(f"Google (browser): failed ({exc})", file=sys.stderr)
                batch = []
            add_many(batch, "Google (browser web)")
    elif serpapi_feature_enabled("google_jobs", cfg) and (only is None or "serpapi" in only or "google_jobs" in only):
        batch = fetch_google_jobs(build_serpapi_queries(cfg), cfg)
        add_many(batch, "SerpAPI: Google Jobs")

    if not uses_browser_search(cfg) and serpapi_feature_enabled("google_site_ats", cfg) and (
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


def _print_no_jobs_after_collect(
    *,
    raw_job_count: int,
    fetch_stats_df: pd.DataFrame,
    cfg: Dict[str, Any],
) -> None:
    """Explain why the job list is empty (quota vs filter vs duplicates), after printing fetch stats."""
    if not fetch_stats_df.empty:
        print("\nSources checked (raw fetch counts):", file=sys.stderr)
        print(fetch_stats_df.to_string(index=False), file=sys.stderr)

    total_fetched = 0
    if not fetch_stats_df.empty and "Fetched" in fetch_stats_df.columns:
        total_fetched = int(pd.to_numeric(fetch_stats_df["Fetched"], errors="coerce").fillna(0).sum())

    if raw_job_count == 0 and total_fetched == 0:
        if uses_browser_search(cfg):
            print(
                "No job rows were retrieved. For LinkedIn: run once "
                "`python3 run.py --linkedin-login` (same --config), then `python3 run.py`. "
                "Add rss_feeds or greenhouse_boards in config for non-LinkedIn sources.",
                file=sys.stderr,
            )
        else:
            print(
                "No job rows were retrieved from any source. If SerpAPI printed HTTP 429 above, your search quota is "
                "exhausted—wait for the plan to reset, increase the SerpAPI limit, or run with other sources only "
                "(for example: --sources rss when rss_feeds is configured).",
                file=sys.stderr,
            )
        return

    if raw_job_count == 0 and total_fetched > 0:
        print(
            "No unique jobs to process: every HTTP response contained only URLs already seen in this run, or "
            "sources returned no parseable listings.",
            file=sys.stderr,
        )
        return

    if cfg.get("filter_jobs_by_location_hint", False):
        print(
            "No jobs left after Israel / location_hint filter. "
            "If location_hint_strict_location_or_title is true, an alias must appear "
            "in the job title or location line (not company/description alone). "
            "Global US boards often produce zero rows — add Israel-focused boards or SerpAPI IL queries.",
            file=sys.stderr,
        )
    else:
        print("No jobs left after configured filters.", file=sys.stderr)


def print_all_queries(cfg: Dict[str, Any]) -> None:
    """Print every Google Jobs + Google web (ATS/LinkedIn) + contact-search query string from config."""
    if uses_browser_search(cfg):
        li = cfg.get("linkedin") if isinstance(cfg.get("linkedin"), dict) else {}
        js = li.get("jobs_search") if isinstance(li.get("jobs_search"), dict) else {}
        print("Search mode: browser (your login — no SerpAPI)\n")
        print("=== LinkedIn Jobs (logged-in browser) ===")
        print(f"  URL: {build_linkedin_jobs_search_url(cfg)}")
        print(f"  keywords: {js.get('keywords', '')}")
        print(f"  location: {js.get('location', '')}")
        print(f"  max_pages: {js.get('max_pages', 3)}")
        print(f"  profile: {resolve_browser_user_data_dir(cfg, service='linkedin')}")
        gw = cfg.get("google_web_browser") if isinstance(cfg.get("google_web_browser"), dict) else {}
        if gw.get("enabled", True):
            print("\n=== Google Web (logged-in browser) ===")
            print(f"  profile: {resolve_browser_user_data_dir(cfg, service='google')}")
            gq = build_google_browser_queries(cfg)
            print(f"  max_queries_per_run: {gw.get('max_queries_per_run', 4)}")
            print(f"  queries ({len(gq)}):")
            for i, q in enumerate(gq, 1):
                print(f"    {i:2d}. {q}")
        print("\n=== Tier 1 (no login) ===")
        print(f"  RSS feeds: {len(cfg.get('rss_feeds') or [])}")
        print(f"  Greenhouse boards: {len(cfg.get('greenhouse_boards') or [])}")
        print(f"  Lever sites: {len(cfg.get('lever_sites') or [])}")
        print("\nFirst-time setup:")
        print("  python3 run.py --linkedin-login")
        print("  python3 run.py --google-login")
        return

    if int(cfg.get("search_version") or 1) >= 2:
        plan = build_search_plan(cfg)
        print(describe_plan_text(plan))
        print()
        print(
            "SerpAPI feature flags:",
            f"google_jobs={serpapi_feature_enabled('google_jobs', cfg)}",
            f"google_site_ats={serpapi_feature_enabled('google_site_ats', cfg)}",
            f"contacts={serpapi_feature_enabled('contacts', cfg)}",
        )
        print("\n(v2 SerpAPI plan — use search_mode: browser to avoid SerpAPI.)")
        return

    gj = build_serpapi_queries(cfg)
    ats = build_ats_google_site_queries(cfg)
    print(
        "SerpAPI feature flags:",
        f"google_jobs={serpapi_feature_enabled('google_jobs', cfg)}",
        f"google_site_ats={serpapi_feature_enabled('google_site_ats', cfg)}",
        f"contacts={serpapi_feature_enabled('contacts', cfg)}",
    )
    print()
    print("=== SerpAPI Google Jobs (engine=google_jobs) — string used as parameter \"q\" ===")
    print("(Runs only when serpapi_features.google_jobs is true.)\n")
    if not gj:
        print("  (none — check serpapi_google_jobs_queries, serpapi_query_template.roles, or serpapi_google_jobs_combine_roles_or.)\n")
    else:
        for i, q in enumerate(gj, 1):
            print(f"  {i:4d}  {q}")
        print(f"\n  Total: {len(gj)}\n")

    print("=== SerpAPI Google Web (engine=google) — string used as parameter \"q\" ===")
    print("(Runs only when ats_google_site_search.enabled and serpapi_features.google_site_ats.)\n")
    if not ats:
        print("  (none — ATS block disabled, or no queries after templates/sites expansion.)\n")
    else:
        for i, q in enumerate(ats, 1):
            print(f"  {i:4d}  {q}")
        print(f"\n  Total: {len(ats)} (after max_queries cap)\n")

    print('=== Recruiter radar (contacts) — Google \"q\" patterns (sample: company=Acme, job_title=DevOps Director) ===')
    print("(Runs when serpapi_features.contacts is true.)\n")
    cq = _build_contact_queries("Acme", "DevOps Director", cfg)
    for i, q in enumerate(cq, 1):
        print(f"  {i:4d}  {q}")
    print(f"\n  Total: {len(cq)}")


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
        help="Comma list: linkedin,google,greenhouse,lever,rss (v1: serpapi,google_site_ats)",
    )
    parser.add_argument(
        "--google-login",
        action="store_true",
        help="Open a browser window for Google (google.co.il profile); helps avoid CAPTCHAs on web search",
    )
    parser.add_argument(
        "--google-login-then-run",
        action="store_true",
        help="Run --google-login (headed), then fetch jobs if login succeeds",
    )
    parser.add_argument(
        "--google-login-wait",
        type=int,
        default=10,
        metavar="MINUTES",
        help="Max minutes to wait for Google login (default 10)",
    )
    parser.add_argument(
        "--google-headed",
        action="store_true",
        help="Run Google web job search with a visible browser (debugging)",
    )
    parser.add_argument(
        "--linkedin-login",
        action="store_true",
        help="Open a browser window to log in to LinkedIn; session saved for later headless runs (browser search mode)",
    )
    parser.add_argument(
        "--linkedin-login-then-run",
        action="store_true",
        help="Run --linkedin-login (headed), then fetch jobs and send email if login succeeds",
    )
    parser.add_argument(
        "--linkedin-login-wait",
        type=int,
        default=10,
        metavar="MINUTES",
        help="Max minutes to wait for manual LinkedIn login (default 10)",
    )
    parser.add_argument(
        "--linkedin-headed",
        action="store_true",
        help="Run LinkedIn Jobs fetch with a visible browser (debugging; same session profile)",
    )
    parser.add_argument("--db", type=Path, default=root / "jobs.db", help="SQLite path")
    parser.add_argument(
        "--allow-non-israel-email",
        action="store_true",
        help="Disable digest_email_enforce_location_hint for this run (digest may include non-Israel rows).",
    )
    parser.add_argument(
        "--email-all-fetched",
        action="store_true",
        help="Email every job fetched this run (not only rows pending digest). DB still dedupes.",
    )
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Fetch sources and store jobs in jobs.db without sending email (for 30-min polling).",
    )
    parser.add_argument(
        "--send-pending-email",
        action="store_true",
        help="Email jobs stored with emailed_at unset (no fetch). Used by scheduled morning/afternoon slots.",
    )
    parser.add_argument(
        "--send-removed-email",
        action="store_true",
        help="Email all jobs you removed (hide list) with a Restore column instead of Remove.",
    )
    parser.add_argument(
        "--digest-slot",
        choices=("morning", "afternoon", "digest", "removed"),
        default="",
        help="Email subject label for scheduled digests (with --send-pending-email or --send-removed-email).",
    )
    parser.add_argument(
        "--print-queries",
        action="store_true",
        help="Print all SerpAPI Google Jobs + Google web (ATS/LinkedIn) + contact-search query strings, then exit",
    )
    parser.add_argument(
        "--log-serpapi-queries",
        action="store_true",
        help="Log each SerpAPI Google Jobs / site:ATS request (full q) to stderr only; does not change which queries run or job results. Same as serpapi_log_each_query: true in config.",
    )
    parser.add_argument(
        "--digest-remove-server",
        action="store_true",
        help="Run local HTTP server for «Remove → Yes» links in digest emails (install LaunchAgent for always-on).",
    )
    parser.add_argument(
        "--init-job-tracker",
        action="store_true",
        help="Create empty job_tracker.xlsx (digest columns + Apply Date, Status).",
    )
    parser.add_argument(
        "--job-tracker-overwrite",
        action="store_true",
        help="Replace existing job tracker workbook when used with --init-job-tracker.",
    )
    parser.add_argument(
        "--set-job-status",
        nargs=2,
        metavar=("LINK", "STATUS"),
        help='Update Status in job_tracker.xlsx (e.g. "Interview", "Rejected").',
    )

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    cfg = {**cfg, "_project_root": str(root)}
    if getattr(args, "set_job_status", None):
        link, status = args.set_job_status
        if not job_tracker_enabled(cfg):
            print("job_tracker is disabled in config.", file=sys.stderr)
            return 1
        try:
            from job_agent.job_tracker_excel import set_job_tracker_status

            canonical = set_job_tracker_status(link, status, cfg, root=root)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            print(f"Allowed: {', '.join(allowed_status_values(cfg))}", file=sys.stderr)
            return 1
        print(f"Updated status for {link!r} → {canonical!r}")
        print(f"Allowed values: {', '.join(allowed_status_values(cfg))}")
        return 0
    if args.init_job_tracker:
        path = default_job_tracker_path(root, cfg)
        try:
            out = create_empty_job_tracker(path, cfg, overwrite=bool(args.job_tracker_overwrite))
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Created job tracker: {out}")
        return 0
    if args.digest_remove_server:
        run_remove_server_forever(cfg)
        return 0
    if args.log_serpapi_queries:
        cfg = {**cfg, "serpapi_log_each_query": True}
    if args.allow_non_israel_email:
        cfg = {**cfg, "digest_email_enforce_location_hint": False}

    if args.google_login or args.google_login_then_run:
        ok = google_login(cfg, wait_minutes=max(1, int(args.google_login_wait)))
        if not args.google_login_then_run:
            return 0 if ok else 1
        if not ok:
            print("Skipping job run — Google login was not confirmed.", file=sys.stderr)
            return 1

    if args.linkedin_login or args.linkedin_login_then_run:
        ok = linkedin_login(cfg, wait_minutes=max(1, int(args.linkedin_login_wait)))
        if not args.linkedin_login_then_run:
            return 0 if ok else 1
        if not ok:
            print("Skipping job run — LinkedIn login was not confirmed.", file=sys.stderr)
            return 1

    if args.google_headed and not uses_browser_search(cfg):
        print("--google-headed requires search_mode: browser in config.", file=sys.stderr)
        return 1

    if args.linkedin_headed and not uses_browser_search(cfg):
        print("--linkedin-headed requires search_mode: browser in config.", file=sys.stderr)
        return 1

    if args.print_queries:
        print_all_queries(cfg)
        return 0

    only = parse_sources_arg(args.sources or None)

    run_cfg = cfg
    if args.linkedin_headed and uses_browser_search(cfg):
        block = dict(cfg.get("browser") or {}) if isinstance(cfg.get("browser"), dict) else {}
        run_cfg = {**cfg, "browser": {**block, "headless": False}}
    if args.google_headed and uses_browser_search(run_cfg):
        gblock = dict(run_cfg.get("google_web_browser") or {}) if isinstance(run_cfg.get("google_web_browser"), dict) else {}
        run_cfg = {**run_cfg, "google_web_browser": {**gblock, "headless": False}}
    if args.fetch_only and uses_browser_search(run_cfg):
        run_cfg = _cfg_disable_reach_out_scrape(run_cfg)

    if args.fetch_only and args.send_pending_email:
        print("Use either --fetch-only or --send-pending-email, not both.", file=sys.stderr)
        return 2
    if args.send_pending_email and args.send_removed_email:
        print("Use either --send-pending-email or --send-removed-email, not both.", file=sys.stderr)
        return 2

    conn = db.connect(args.db)
    try:
        slot = (args.digest_slot or "").strip()
        subject = _digest_subject(cfg, slot) if slot else None
        empty_stats = pd.DataFrame()

        if args.send_removed_email:
            return _send_removed_jobs_email(cfg=cfg, root=root, args=args, conn=conn)

        if args.send_pending_email:
            email_jobs = _jobs_for_scheduled_digest(conn, cfg)
            pending_n = len(db.load_pending_jobs(conn)) if _digest_only_new(cfg) else len(db.load_all_stored_jobs(conn))
            if _digest_only_new(cfg):
                note = (
                    f"Scheduled digest ({slot or 'pending'}): {len(email_jobs)} new job(s) to email. "
                    "Counts in «Sources checked» appear only on fetch runs."
                )
            else:
                within = float(cfg.get("digest_include_jobs_seen_within_days") or 2)
                note = (
                    f"Scheduled digest ({slot or 'digest'}): {len(email_jobs)} unique job(s) "
                    f"seen in the last {within:g} day(s). "
                    "Set digest_ignore_links / digest_ignore_companies in config to hide specific jobs later."
                )
            print(note)
            if not email_jobs:
                if not args.dry_run:
                    print("No jobs to email after filters.")
                return 0
            return _send_email_for_jobs(
                email_jobs=email_jobs,
                cfg=cfg,
                root=root,
                args=args,
                conn=conn,
                fetch_stats_df=empty_stats,
                digest_note="",
                subject=subject,
            )

        jobs, fetch_stats_df, raw_job_count, location_dropped = _collect_and_filter_jobs(run_cfg, cfg, only)
        if not jobs:
            _print_no_jobs_after_collect(
                raw_job_count=raw_job_count,
                fetch_stats_df=fetch_stats_df,
                cfg=cfg,
            )
            return 0

        stored_new = 0
        if not args.dry_run:
            stored_new = db.upsert_jobs(conn, jobs, mark_emailed=False)

        pending_count = len(db.load_pending_jobs(conn))
        print(
            f"Fetch: {len(jobs)} job(s) after filters ({location_dropped} dropped by location); "
            f"{stored_new} new in jobs.db; {pending_count} pending email.",
        )
        if not fetch_stats_df.empty:
            print("\nSources checked (raw fetch counts):", file=sys.stderr)
            print(fetch_stats_df.to_string(index=False), file=sys.stderr)

        if args.fetch_only:
            return 0

        only_new = _digest_only_new(cfg) and not args.email_all_fetched
        if args.email_all_fetched or not only_new:
            email_jobs = _finalize_jobs_for_digest(list(jobs), cfg)
        else:
            email_jobs = _finalize_jobs_for_digest(db.load_pending_jobs(conn), cfg)

        if not email_jobs:
            if only_new:
                print("No jobs to email (nothing pending). Use --email-all-fetched or set digest_email_only_new: false.")
            else:
                print("No jobs to email after filters.")
            return 0

        return _send_email_for_jobs(
            email_jobs=email_jobs,
            cfg=cfg,
            root=root,
            args=args,
            conn=conn,
            fetch_stats_df=fetch_stats_df,
            digest_note="",
            subject=subject,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(run())
