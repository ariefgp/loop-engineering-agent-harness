from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from .config import RepositoryConfig
from .models import Candidate, Role, deduplicate_and_sort


class GitHubError(RuntimeError):
    pass


_STATE_ROLES: tuple[tuple[str, Role], ...] = (
    ("to be planned", Role.PM),
    ("in progress", Role.DEV),
    ("feedback", Role.DEV),
    ("todo", Role.DEV),
    ("qa in progress", Role.QA),
    ("qa ready", Role.QA),
)
_PRIORITY_NAMES = {"urgent", "p0", "high", "p1", "medium", "p2", "low"}
_WORKFLOW_STATES = {
    "to be planned",
    "plan approval",
    "need confirmation",
    "todo",
    "in progress",
    "qa ready",
    "qa in progress",
    "feedback",
    "review ready",
    "done",
    "hold",
}


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubError(f"GitHub command failed: {exc}") from exc


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise GitHubError("GitHub issue is missing updatedAt")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise GitHubError(f"invalid GitHub timestamp: {value!r}") from exc


class GitHubClient:
    def __init__(
        self,
        *,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _default_runner,
    ) -> None:
        self._runner = runner

    def scan(self, repositories: Iterable[RepositoryConfig]) -> list[Candidate]:
        candidates: list[Candidate] = []
        eligible = dict(_STATE_ROLES)
        for repo in repositories:
            if not repo.enabled:
                continue
            argv = [
                "gh",
                "issue",
                "list",
                "--repo",
                repo.slug,
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,title,labels,createdAt,updatedAt",
            ]
            result = self._runner(argv)
            if result.returncode != 0:
                detail = result.stderr.strip() or f"exit {result.returncode}"
                raise GitHubError(f"{repo.slug}: {detail}")
            try:
                rows = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise GitHubError(f"{repo.slug}: invalid JSON") from exc
            if not isinstance(rows, list):
                raise GitHubError(f"{repo.slug}: expected issue list")

            for row in rows:
                if not isinstance(row, dict):
                    raise GitHubError(f"{repo.slug}: invalid issue row")
                labels = row.get("labels", [])
                label_names = [
                    str(label.get("name", ""))
                    for label in labels
                    if isinstance(label, dict)
                ]
                state_labels = [
                    name.casefold() for name in label_names if name.casefold() in _WORKFLOW_STATES
                ]
                if len(state_labels) != 1:
                    continue
                state = state_labels[0]
                role = eligible.get(state)
                if role is None or role not in repo.roles:
                    continue
                priorities = [
                    name for name in label_names if name.casefold() in _PRIORITY_NAMES
                ]
                if len(priorities) > 1:
                    continue
                priority = priorities[0] if priorities else "missing"
                try:
                    number = int(row["number"])
                    title = str(row["title"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise GitHubError(f"{repo.slug}: malformed issue") from exc
                candidates.append(
                    Candidate(
                        repo=repo.slug,
                        repo_path=repo.path,
                        number=number,
                        title=title,
                        role=role,
                        state=state,
                        priority=priority,
                        state_entered_at=_parse_time(row.get("updatedAt")),
                    )
                )
        return deduplicate_and_sort(candidates)
