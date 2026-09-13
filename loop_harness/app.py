from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import RepositoryConfig
from .models import Candidate, Role
from .runtime import FileLock, RuntimePaths
from .scheduler import Assignment, DEFAULT_WORKERS, WorkerSpec, plan_assignments, run_concurrently
from .store import RunStore
from .worker import WorkerResult, WorkerRunner, linux_process_start


_GITHUB_PATH = r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/((?P<repo>[A-Za-z0-9_.-]+?))(?:\.git)?"
_GITHUB_HTTPS_ORIGIN = re.compile(rf"^https://github\.com/{_GITHUB_PATH}$")
_GITHUB_SCP_ORIGIN = re.compile(rf"^git@github\.com:{_GITHUB_PATH}$")
_GITHUB_SSH_ORIGIN = re.compile(rf"^ssh://git@github\.com/{_GITHUB_PATH}$")
_HARNESS_ROOT = Path(__file__).resolve().parent.parent


def _github_slug(origin: str) -> str | None:
    for pattern in (
        _GITHUB_HTTPS_ORIGIN,
        _GITHUB_SCP_ORIGIN,
        _GITHUB_SSH_ORIGIN,
    ):
        match = pattern.fullmatch(origin)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def validate_repository_origin(repo_path: Path, expected_slug: str) -> None:
    url_sets: list[list[str]] = []
    for extra in ([], ["--push"]):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "remote",
                "get-url",
                "--all",
                *extra,
                "origin",
            ],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(f"cannot read git origin for {repo_path}")
        urls = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        url_sets.append(urls)

    if any(len(urls) != 1 for urls in url_sets):
        raise ValueError(
            f"repository origin mismatch for {repo_path}: expected exactly one "
            "canonical fetch URL and one canonical push URL"
        )
    actual_slugs = [_github_slug(urls[0]) for urls in url_sets]
    if any(
        actual is None or actual.casefold() != expected_slug.casefold()
        for actual in actual_slugs
    ):
        raise ValueError(
            f"repository origin mismatch for {repo_path}: expected {expected_slug}, "
            "got a noncanonical or different GitHub origin"
        )


def validate_repository_contracts(
    candidates: Iterable[Candidate],
    workers: Sequence[WorkerSpec],
) -> None:
    roles_by_path: dict[Path, set[Role]] = {}
    for candidate in candidates:
        roles_by_path.setdefault(candidate.repo_path.resolve(), set()).add(candidate.role)
    for repo_path, roles in roles_by_path.items():
        required = {".agents/WORKFLOW.md"}
        required.update(worker.contract for worker in workers if worker.role in roles)
        for relative in sorted(required):
            source = _HARNESS_ROOT / relative
            target = repo_path / relative
            if not target.is_file() or target.read_bytes() != source.read_bytes():
                raise ValueError(
                    f"contract {relative} is not synchronized in {repo_path}"
                )


@dataclass(frozen=True)
class TickResult:
    mode: str
    assignments: list[Assignment]
    results: list[WorkerResult]
    reclaimed: list[str]


def process_is_alive(pid: int, expected_start: str) -> bool:
    if expected_start == "unknown":
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return linux_process_start(pid) == expected_start


class Harness:
    def __init__(
        self,
        *,
        github,
        repositories: Iterable[RepositoryConfig],
        paths: RuntimePaths,
        worker_runner: WorkerRunner,
        workers: Sequence[WorkerSpec] = DEFAULT_WORKERS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        worker_timeout_seconds: float = 3000,
        enforce_contracts: bool = True,
    ) -> None:
        self.github = github
        self.repositories = list(repositories)
        self.paths = paths
        self.worker_runner = worker_runner
        self.workers = tuple(workers)
        self.now = now
        self.worker_timeout_seconds = worker_timeout_seconds
        self.enforce_contracts = enforce_contracts

    def tick(self, *, dry_run: bool = False) -> TickResult:
        candidates = self.github.scan(self.repositories)
        if self.enforce_contracts:
            validate_repository_contracts(candidates, self.workers)
        if dry_run:
            busy_profiles: set[str] = set()
            active_issues: set[tuple[str, int]] = set()
            if self.paths.database.is_file():
                shadow_store = RunStore(self.paths.database, read_only=True)
                try:
                    busy_profiles = shadow_store.active_profiles()
                    active_issues = shadow_store.active_issues()
                finally:
                    shadow_store.close()
            planned = plan_assignments(
                candidates,
                self.workers,
                busy_profiles=busy_profiles,
                active_issues=active_issues,
            )
            return TickResult("dry-run", planned, [], [])

        for candidate in {item.identity: item for item in candidates}.values():
            validate_repository_origin(candidate.repo_path, candidate.repo)

        self.paths.ensure()
        store = RunStore(self.paths.database)
        reservations: list[tuple[str, Assignment]] = []
        with FileLock(self.paths.claim_lock):
            reclaimed = store.reclaim_stale(self.now(), process_is_alive)
            reserved_profiles: set[str] = set()
            reserved_issues: set[tuple[str, int]] = set()
            conflicted_issues: set[tuple[str, int]] = set()
            while True:
                planned = plan_assignments(
                    candidates,
                    self.workers,
                    busy_profiles=store.active_profiles() | reserved_profiles,
                    active_issues=(
                        store.active_issues() | reserved_issues | conflicted_issues
                    ),
                )
                if not planned:
                    break
                for item in planned:
                    run_id = f"run-{uuid.uuid4().hex}"
                    if store.reserve(item, run_id, self.now()):
                        reservations.append((run_id, item))
                        reserved_profiles.add(item.worker.profile)
                        reserved_issues.add(item.candidate.identity)
                    else:
                        conflicted_issues.add(item.candidate.identity)

        def execute(reservation: tuple[str, Assignment]) -> WorkerResult:
            run_id, item = reservation
            try:
                result = self.worker_runner.run(
                    run_id,
                    item,
                    timeout=self.worker_timeout_seconds,
                    on_started=lambda pid, start: store.mark_running(
                        run_id, pid=pid, process_start=start, at=self.now()
                    ),
                    on_heartbeat=lambda: store.heartbeat(run_id, self.now()),
                )
            except Exception:
                store.finish(run_id, "launch-failed")
                raise
            store.finish(run_id, result.status, result.result_path)
            return result

        results = run_concurrently(reservations, execute)
        return TickResult("live", [item for _, item in reservations], results, reclaimed)
