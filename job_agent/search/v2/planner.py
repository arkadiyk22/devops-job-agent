"""v2 search plan: tier-1 sources + budget-limited SerpAPI queries (design stub)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from job_agent.search.v2.profile import SearchProfile, get_profile
from job_agent.search.v2.queries import google_jobs_queries, google_web_queries


@dataclass
class SearchPlan:
    profile: str
    serpapi_max_calls: int
    google_jobs_queries: List[str] = field(default_factory=list)
    google_web_queries: List[str] = field(default_factory=list)
    rss_feeds: List[str] = field(default_factory=list)
    greenhouse_boards: List[str] = field(default_factory=list)
    lever_sites: List[str] = field(default_factory=list)
    location_filter_mode: str = "israel_or_il_signals"

    @property
    def estimated_serpapi_calls(self) -> int:
        """Rough count if every planned query runs once (hl variants not included yet)."""
        return len(self.google_jobs_queries) + len(self.google_web_queries)


def _v2_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    block = cfg.get("search_v2")
    return block if isinstance(block, dict) else {}


def build_search_plan(cfg: Dict[str, Any]) -> SearchPlan:
    """Build v2 plan from config. Explicit ``search_v2.*`` overrides profile defaults."""
    profile_name = str(cfg.get("search_profile") or "israel_devops_leadership")
    profile = get_profile(profile_name)
    block = _v2_block(cfg)

    gj_block = block.get("google_jobs") if isinstance(block.get("google_jobs"), dict) else {}
    gw_block = block.get("google_web") if isinstance(block.get("google_web"), dict) else {}

    gj_explicit = gj_block.get("queries")
    if isinstance(gj_explicit, list) and gj_explicit:
        gj = [str(q).strip() for q in gj_explicit if str(q).strip()]
    else:
        include_he = gj_block.get("include_hebrew", True)
        gj = google_jobs_queries(profile, include_hebrew=bool(include_he))

    gw_explicit = gw_block.get("queries")
    if isinstance(gw_explicit, list) and gw_explicit:
        gw = [str(q).strip() for q in gw_explicit if str(q).strip()]
    else:
        gw = google_web_queries(profile)

    max_calls = int(cfg.get("serpapi_max_calls_per_run") or 5)
    loc_mode = str(cfg.get("location_filter_mode") or "israel_or_il_signals")

    return SearchPlan(
        profile=profile_name,
        serpapi_max_calls=max(1, max_calls),
        google_jobs_queries=gj,
        google_web_queries=gw,
        rss_feeds=[str(u).strip() for u in (cfg.get("rss_feeds") or []) if str(u).strip()],
        greenhouse_boards=[str(b).strip() for b in (cfg.get("greenhouse_boards") or []) if str(b).strip()],
        lever_sites=[str(s).strip() for s in (cfg.get("lever_sites") or []) if str(s).strip()],
        location_filter_mode=loc_mode,
    )


def describe_plan_text(plan: SearchPlan) -> str:
    """Human-readable plan for ``--print-queries`` (v2, when wired)."""
    lines = [
        f"Search profile: {plan.profile}",
        f"SerpAPI budget: {plan.serpapi_max_calls} calls/run (planned ~{plan.estimated_serpapi_calls} if all queries run once)",
        f"Location filter mode (v2): {plan.location_filter_mode}",
        "",
        "=== Tier 1 (no SerpAPI) ===",
        f"  RSS feeds: {len(plan.rss_feeds)}",
        f"  Greenhouse boards: {len(plan.greenhouse_boards)}",
        f"  Lever sites: {len(plan.lever_sites)}",
        "",
        "=== SerpAPI Google Jobs ===",
    ]
    for i, q in enumerate(plan.google_jobs_queries, 1):
        lines.append(f"  {i:3d}  {q}")
    lines.append(f"\n  Total: {len(plan.google_jobs_queries)}\n")
    lines.append("=== SerpAPI Google Web (site:) ===")
    for i, q in enumerate(plan.google_web_queries, 1):
        lines.append(f"  {i:3d}  {q}")
    lines.append(f"\n  Total: {len(plan.google_web_queries)}")
    return "\n".join(lines)
