from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable


class Role(str, Enum):
    PM = "pm"
    DEV = "dev"
    QA = "qa"


_RESUME_STATES = {"in progress", "qa in progress"}
_PRIORITY_RANK = {
    "urgent": 0,
    "p0": 0,
    "high": 1,
    "p1": 1,
    "medium": 2,
    "p2": 2,
    "low": 3,
}


@dataclass(frozen=True)
class Candidate:
    repo: str
    repo_path: Path
    number: int
    title: str
    role: Role
    state: str
    priority: str
    state_entered_at: datetime

    @property
    def identity(self) -> tuple[str, int]:
        return self.repo, self.number

    @property
    def sort_key(self) -> tuple[int, int, datetime, str, int]:
        return (
            0 if self.state.casefold() in _RESUME_STATES else 1,
            _PRIORITY_RANK.get(self.priority.casefold(), 9),
            self.state_entered_at,
            self.repo.casefold(),
            self.number,
        )


def deduplicate_and_sort(candidates: Iterable[Candidate]) -> list[Candidate]:
    selected: dict[tuple[str, int], Candidate] = {}
    for item in candidates:
        current = selected.get(item.identity)
        if current is None or item.sort_key < current.sort_key:
            selected[item.identity] = item
    return sorted(selected.values(), key=lambda item: item.sort_key)
