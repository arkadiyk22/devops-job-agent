"""Build short boolean Google / site: query strings for v2."""

from __future__ import annotations

from typing import List

from job_agent.search.v2.profile import SearchProfile


def _or_quoted(phrases: List[str]) -> str:
    seen: set[str] = set()
    bits: List[str] = []
    for p in phrases:
        t = (p or "").strip()
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        bits.append(f'"{t}"')
    return " OR ".join(bits)


def google_jobs_queries(profile: SearchProfile, *, include_hebrew: bool = True) -> List[str]:
    """At most two Google Jobs ``q`` strings: EN block + optional HE block, each ending with geo."""
    geo = profile.geo_token.strip().lower()
    out: List[str] = []
    en = _or_quoted(profile.roles_en)
    if en:
        out.append(f"({en}) {geo}")
    if include_hebrew and profile.roles_he:
        he = _or_quoted(profile.roles_he)
        if he:
            out.append(f"({he}) {geo}")
    return out


def google_web_queries(profile: SearchProfile) -> List[str]:
    """Default v2 web queries: IL LinkedIn jobs + Workable Israel."""
    en = _or_quoted(profile.roles_en[:4])
    mix = _or_quoted(profile.roles_en[:2] + profile.roles_he[:1])
    geo = "israel"
    return [
        f"site:il.linkedin.com/jobs ({mix}) {geo}",
        f"site:apply.workable.com {geo} ({en})",
    ]
