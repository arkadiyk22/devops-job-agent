"""Named search profiles for v2 (Israel DevOps leadership by default)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class SearchProfile:
    name: str
    roles_en: List[str]
    roles_he: List[str]
    exclude_role_patterns: List[str]
    geo_token: str = "israel"


DEFAULT_PROFILE = SearchProfile(
    name="israel_devops_leadership",
    roles_en=[
        "devops manager",
        "devops director",
        "head of devops",
        "director of devops",
        "vp devops",
        "platform engineering manager",
        "sre manager",
    ],
    roles_he=[
        "מנהל devops",
        "מנהלת devops",
        "מנהל דבאופס",
    ],
    exclude_role_patterns=[
        "team lead",
        "devops lead",
        "lead devops",
    ],
)


def get_profile(name: str) -> SearchProfile:
    if name == DEFAULT_PROFILE.name:
        return DEFAULT_PROFILE
    raise ValueError(f"Unknown search profile: {name!r} (only {DEFAULT_PROFILE.name!r} for now)")
