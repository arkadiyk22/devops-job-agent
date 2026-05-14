from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class Job:
    source: str
    company: str
    title: str
    location: str
    link: str
    posted: str = "recent"
    score: int = 0
    search_fallback: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> Dict[str, Any]:
        return {
            "Job Title": self.title,
            "Company": self.company,
            "Link": self.link,
            "Recommended Search": self.search_fallback,
            "Source": self.source,
            "Posted Date": self.posted,
            "Location": self.location,
            "Score": self.score,
        }
