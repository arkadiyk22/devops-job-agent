"""Build SerpAPI Google Jobs and Google Web (``site:`` ATS) query strings."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List

DEFAULT_ATS_SITE_HOSTS = (
    "comeet.co",
    "greenhouse.io",
    "myworkdayjobs.com",
    "lever.co",
    "apply.workable.com",
    "smartrecruiters.com",
    "linkedin.com",
    "il.linkedin.com",
)

# Extra OR-blocks prepended to ``role_phrases`` (Google ``site:`` / ATS web search).
BUILTIN_DEVOPS_OR_BLOCKS = (
    '("devops manager" OR "devops director" OR "head of devops" OR "director of devops" OR "vp devops")',
    '("engineering manager devops" OR "devops team lead" OR "lead devops" OR "devops lead")',
    '("platform engineering manager" OR "director of platform engineering" OR "head of platform engineering")',
    '("sre manager" OR "site reliability manager" OR "infrastructure manager" OR "cloud operations manager")',
    '(מנהל DevOps OR מנהלת DevOps OR "מנהל דבאופס" OR "מנהלת דבאופס" OR "אחראי DevOps" OR "אחראית DevOps")',
    '(ראש צוות DevOps OR "ראש צוות דבאופס" OR "מוביל DevOps" OR "מובילת DevOps")',
    '("מנהל תשתיות ענן" OR "מנהלת תשתיות ענן" OR "מנהל פלטפורמה" OR "מנהלת פלטפורמה" OR "מנהל תפעול ענן")',
    '("מנהל CI/CD" OR "מנהל Kubernetes" OR "מנהל אוטומציה" OR "מנהל SRE" OR "מנהלת SRE")',
)

DEFAULT_ATS_ROLE_PHRASES = (
    '("devops manager" OR "devops director" OR "head of devops")',
    '("platform engineering manager" OR "director of devops" OR "vp devops")',
    "(מנהל DevOps OR מנהלת DevOps OR \"מנהל דבאופס\")",
)

DEFAULT_ATS_GEO_SUFFIXES = ("Israel", "Tel Aviv", "ישראל")

# פירוט-style Google patterns (DevOps/Israel; placeholders expanded at runtime).
# LinkedIn: organic links to ``/jobs/view/`` are ingested when URL filter matches.
DEFAULT_EXTRA_QUERY_TEMPLATES = (
    'site:www.comeet.com/jobs {roles_core} {geo}{after}',
    "site:myworkdayjobs.com {roles_core} {geo}{after}",
    "site:apply.workable.com {roles_core} {geo}{after}",
    'site:job-boards.greenhouse.io "Tel Aviv" {roles_core}{after}',
    'site:apply.workable.com {geo} {roles_wide}{after}',
    'site:job-boards.greenhouse.io {geo} {roles_core}{after}',
    "site:linkedin.com/jobs/view {roles_core} {geo}{after}",
    "site:il.linkedin.com/jobs {roles_core} {geo}{after}",
    'site:linkedin.com/jobs {roles_wide} {geo}{after}',
)


def build_serpapi_queries(cfg: Dict[str, Any]) -> List[str]:
    """Prefer explicit ``serpapi_google_jobs_queries``; else expand ``serpapi_query_template``."""
    explicit = cfg.get("serpapi_google_jobs_queries")
    if isinstance(explicit, list) and len(explicit) > 0:
        return [str(q).strip() for q in explicit if str(q).strip()]

    tpl = cfg.get("serpapi_query_template")
    if not isinstance(tpl, dict):
        return []

    roles = [str(r).strip() for r in (tpl.get("roles") or []) if str(r).strip()]
    suffixes = [str(s).strip() for s in (tpl.get("suffixes") or []) if str(s).strip()]
    if not roles:
        return []
    if not suffixes:
        return roles

    seen: set[str] = set()
    out: List[str] = []
    for role in roles:
        for suf in suffixes:
            q = f"{role} {suf}".strip()
            if q not in seen:
                seen.add(q)
                out.append(q)
    return out


def _roles_core_wide(role_phrases: List[str]) -> tuple[str, str]:
    core = role_phrases[0] if role_phrases else DEFAULT_ATS_ROLE_PHRASES[0]
    wide = role_phrases[1] if len(role_phrases) > 1 else core
    return core, wide


def _expand_extra_query_templates(
    b: Dict[str, Any],
    geos: List[str],
    after_clause: str,
    role_phrases: List[str],
    seen: set[str],
    out: List[str],
) -> None:
    """Append פירוט-style ``site:`` patterns (Comeet path, job-boards GH, Workable OR-blocks, …)."""
    roles_core, roles_wide = _roles_core_wide(role_phrases)

    templates: List[str] = []
    if b.get("include_builtin_extra_query_templates", True):
        templates.extend(DEFAULT_EXTRA_QUERY_TEMPLATES)
    custom = b.get("extra_query_templates")
    if isinstance(custom, list):
        for line in custom:
            t = str(line).strip()
            if t:
                templates.append(t)

    tpl_seen: set[str] = set()
    for raw_tpl in templates:
        if raw_tpl in tpl_seen:
            continue
        tpl_seen.add(raw_tpl)

        if "{geo}" in raw_tpl:
            geo_list = geos
        else:
            geo_list = [""]

        for geo in geo_list:
            q = (
                raw_tpl.replace("{after}", after_clause)
                .replace("{roles_core}", roles_core)
                .replace("{roles_wide}", roles_wide)
                .replace("{geo}", geo)
            )
            q = " ".join(q.split()).strip()
            if q and q not in seen:
                seen.add(q)
                out.append(q)


def build_ats_google_site_queries(cfg: Dict[str, Any]) -> List[str]:
    """Build ``site:<ats-host> …`` queries for SerpAPI ``engine=google`` (organic results).

    Includes **פירוט**-style patterns (``DEFAULT_EXTRA_QUERY_TEMPLATES``) plus optional
    ``ats_google_site_search.extra_query_templates``. Placeholders:

    - ``{geo}`` — each entry from ``geo_suffixes`` (omit placeholder for a single global query).
    - ``{after}`` — Google ``after:YYYY-MM-DD`` slice (or empty if disabled).
    - ``{roles_core}`` / ``{roles_wide}`` — first/second ``role_phrases`` OR-block (DevOps-focused).
    """
    b = cfg.get("ats_google_site_search")
    if not isinstance(b, dict) or not b.get("enabled", False):
        return []

    raw_sites = b.get("sites")
    if isinstance(raw_sites, list) and raw_sites:
        sites: List[str] = []
        for item in raw_sites:
            if isinstance(item, dict) and item.get("host"):
                sites.append(str(item["host"]).strip())
            elif isinstance(item, str) and item.strip():
                sites.append(item.strip())
    else:
        sites = list(DEFAULT_ATS_SITE_HOSTS)

    role_phrases = b.get("role_phrases")
    if not isinstance(role_phrases, list) or not role_phrases:
        role_phrases = list(DEFAULT_ATS_ROLE_PHRASES)
    else:
        role_phrases = [str(x).strip() for x in role_phrases if str(x).strip()]

    if b.get("prepend_builtin_devops_or_blocks", True):
        merged: List[str] = []
        seen_rp: set[str] = set()
        for block in BUILTIN_DEVOPS_OR_BLOCKS:
            if block not in seen_rp:
                seen_rp.add(block)
                merged.append(block)
        for rp in role_phrases:
            if rp not in seen_rp:
                seen_rp.add(rp)
                merged.append(rp)
        role_phrases = merged

    if b.get("merge_roles_from_serpapi_template", True):
        tpl = cfg.get("serpapi_query_template")
        if isinstance(tpl, dict):
            roles = [str(r).strip() for r in (tpl.get("roles") or []) if str(r).strip()][:12]
            if len(roles) >= 2:
                orq = "(" + " OR ".join(f'"{r}"' for r in roles) + ")"
                if orq not in role_phrases:
                    role_phrases = list(role_phrases) + [orq]

    geos = b.get("geo_suffixes")
    if not isinstance(geos, list) or not geos:
        geos = list(DEFAULT_ATS_GEO_SUFFIXES)
    else:
        geos = [str(x).strip() for x in geos if str(x).strip()]

    days = int(b.get("after_days_ago", 30) or 30)
    buffer = int(b.get("after_date_buffer_days", 5) or 0)
    after_cutoff = date.today() - timedelta(days=min(max(days + buffer, 1), 365))
    after_clause = f" after:{after_cutoff.isoformat()}"
    if b.get("omit_after_clause", False):
        after_clause = ""

    seen: set[str] = set()
    out: List[str] = []

    _expand_extra_query_templates(b, geos, after_clause, role_phrases, seen, out)

    for host in sites:
        if not host:
            continue
        host = host.lstrip("@").strip()
        for rp in role_phrases:
            for geo in geos:
                q = f"site:{host} {rp} {geo}{after_clause}".strip()
                if q not in seen:
                    seen.add(q)
                    out.append(q)

    max_q = int(b.get("max_queries", 0) or 0)
    if max_q > 0:
        out = out[:max_q]
    return out
