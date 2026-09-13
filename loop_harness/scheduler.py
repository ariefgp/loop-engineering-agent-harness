from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from .models import Candidate, Role, deduplicate_and_sort


@dataclass(frozen=True)
class WorkerSpec:
    profile: str
    role: Role
    contract: str
    executable: str | None = None

    def __post_init__(self) -> None:
        if self.executable is None:
            object.__setattr__(self, "executable", self.profile)


@dataclass(frozen=True)
class Assignment:
    worker: WorkerSpec
    candidate: Candidate


DEFAULT_WORKERS = (
    WorkerSpec("gibbs", Role.PM, ".agents/agent-pm.md"),
    WorkerSpec("mcgee", Role.DEV, ".agents/agent-dev.md"),
    WorkerSpec("torres", Role.DEV, ".agents/agent-dev-torres.md"),
    WorkerSpec("kate", Role.DEV, ".agents/agent-dev-kate.md"),
    WorkerSpec("jimmy", Role.QA, ".agents/agent-qa.md"),
    WorkerSpec("ducky", Role.QA, ".agents/agent-qa-ducky.md"),
)


def build_profile_argv(
    worker: WorkerSpec,
    repo_path: Path,
    *,
    run_budget_seconds: int,
) -> list[str]:
    return [
        str(worker.executable),
        "chat",
        "--query-file",
        "-",
        "--oneshot",
        "-Q",
        "--in",
        str(repo_path),
        "--run-budget",
        str(run_budget_seconds),
    ]


def plan_assignments(
    candidates: Iterable[Candidate],
    workers: Iterable[WorkerSpec] = DEFAULT_WORKERS,
    *,
    busy_profiles: set[str],
    active_issues: set[tuple[str, int]] | None = None,
) -> list[Assignment]:
    excluded_issues = active_issues or set()
    queue = [
        item for item in deduplicate_and_sort(candidates) if item.identity not in excluded_issues
    ]
    used_issues: set[tuple[str, int]] = set()
    assignments: list[Assignment] = []
    for worker in workers:
        if worker.profile in busy_profiles:
            continue
        match = next(
            (
                item
                for item in queue
                if item.role is worker.role and item.identity not in used_issues
            ),
            None,
        )
        if match is not None:
            assignments.append(Assignment(worker, match))
            used_issues.add(match.identity)
    return assignments


_Item = TypeVar("_Item")
_Result = TypeVar("_Result")


def run_concurrently(
    assignments: Iterable[_Item],
    execute: Callable[[_Item], _Result],
) -> list[_Result]:
    work = list(assignments)
    if not work:
        return []
    with ThreadPoolExecutor(max_workers=len(work), thread_name_prefix="loop-worker") as pool:
        futures = [pool.submit(execute, item) for item in work]
        return [future.result() for future in futures]
