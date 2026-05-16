"""SerpAPI call budget for v2 (prevent burning monthly quota in one run)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SerpapiBudget:
    max_calls: int
    used: int = 0

    def can_spend(self, n: int = 1) -> bool:
        return self.used + n <= self.max_calls

    def spend(self, n: int = 1) -> bool:
        if not self.can_spend(n):
            return False
        self.used += n
        return True

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.used)
