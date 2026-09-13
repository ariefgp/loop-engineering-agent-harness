from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence, cast

from .config import RepositoryConfig
from .models import Candidate, ResolvedSource, Role, WorkspaceContext
from .runtime import FileLock, ResultStore, RuntimePaths, redact_text
from .scheduler import Assignment, DEFAULT_WORKERS, WorkerSpec, plan_assignments, run_concurrently
from .store import RunStore
from .worker import WorkerResult, WorkerRunner, linux_process_start
from .workspace import GitObjectObservation, WorkspaceManager, WorkspaceOutcome


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


def validate_repository_origin(repo_path: Path, expected_slug: str) -> str:
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
    return url_sets[0][0]


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


def _required_contracts(_assignment: Assignment) -> tuple[str, ...]:
    return tuple(sorted({".agents/WORKFLOW.md", *(worker.contract for worker in DEFAULT_WORKERS)}))


def validate_assignment_contracts(assignment: Assignment) -> None:
    if assignment.workspace is None:
        raise ValueError("assignment has no dispatcher workspace")
    for relative in _required_contracts(assignment):
        source = _HARNESS_ROOT / relative
        target = assignment.workspace.path / relative
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            raise ValueError(
                f"contract {relative} is not synchronized at assigned revision"
            )


@dataclass(frozen=True)
class RepositoryFailure:
    repository: str
    stage: str
    error: str


@dataclass(frozen=True)
class TickResult:
    mode: str
    assignments: list[Assignment]
    results: list[WorkerResult]
    reclaimed: list[str]
    failures: list[RepositoryFailure] = field(default_factory=list)


def process_is_alive(pid: int, expected_start: str) -> bool:
    if expected_start == "unknown":
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return linux_process_start(pid) == expected_start


def _context_to_dict(context: WorkspaceContext) -> dict[str, object]:
    return {
        "path": str(context.path),
        "source_sha": context.source_sha,
        "source_kind": context.source_kind,
        "local_branch": context.local_branch,
        "remote_branch": context.remote_branch,
        "pr_number": context.pr_number,
        "private_ref": context.private_ref,
        "run_root": str(context.run_root),
        "git_dir": str(context.git_dir),
        "expected_origin_url": context.expected_origin_url,
        "baseline_commits": list(context.baseline_commits),
        "repo_slug": context.repo_slug,
        "observed_objects": list(context.observed_objects),
        "audit_path": str(context.audit_path) if context.audit_path is not None else None,
        "observation_error": context.observation_error,
    }


def _context_from_dict(value: dict[str, object]) -> WorkspaceContext:
    local_branch = value.get("local_branch")
    remote_branch = value.get("remote_branch")
    pr_number = value.get("pr_number")
    baseline = value.get("baseline_commits", [])
    observed = value.get("observed_objects", [])
    if local_branch is not None and not isinstance(local_branch, str):
        raise ValueError("invalid workspace local branch")
    if remote_branch is not None and not isinstance(remote_branch, str):
        raise ValueError("invalid workspace remote branch")
    if pr_number is not None and type(pr_number) is not int:
        raise ValueError("invalid workspace pull request")
    if not isinstance(baseline, list):
        raise ValueError("invalid workspace baseline")
    if not isinstance(observed, list):
        raise ValueError("invalid observed Git objects")
    return WorkspaceContext(
        path=Path(str(value["path"])),
        source_sha=str(value["source_sha"]),
        source_kind=str(value["source_kind"]),
        local_branch=local_branch,
        remote_branch=remote_branch,
        pr_number=pr_number,
        private_ref=str(value["private_ref"]),
        run_root=Path(str(value["run_root"])),
        git_dir=Path(str(value["git_dir"])),
        expected_origin_url=str(value["expected_origin_url"]),
        baseline_commits=tuple(str(item) for item in baseline),
        repo_slug=str(value.get("repo_slug", "")),
        observed_objects=tuple(str(item) for item in observed),
        audit_path=(Path(str(value["audit_path"])) if value.get("audit_path") else None),
        observation_error=(
            str(value["observation_error"]) if value.get("observation_error") else None
        ),
    )


def _outcome_to_dict(outcome: WorkspaceOutcome) -> dict[str, object]:
    return {
        "final_sha": outcome.final_sha,
        "clean": outcome.clean,
        "push_verified": outcome.push_verified,
        "handoff_verified": outcome.handoff_verified,
        "cleanup": outcome.cleanup,
        "failure": outcome.failure,
    }


def _canonicalize_terminal_payload(
    payload: dict[str, object], outcome: str
) -> None:
    payload["status"] = outcome
    payload["terminal_status"] = outcome
    if outcome == "completed":
        payload["exit_code"] = 0
        payload.pop("failure_stage", None)
        payload.pop("error", None)
    else:
        payload["exit_code"] = 1


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
        workspace_manager: WorkspaceManager | None = None,
    ) -> None:
        self.github = github
        self.repositories = list(repositories)
        self.paths = paths
        self.worker_runner = worker_runner
        self.workers = tuple(workers)
        self.now = now
        self.worker_timeout_seconds = worker_timeout_seconds
        self.enforce_contracts = enforce_contracts
        self.workspace_manager = workspace_manager or WorkspaceManager(
            Path.home() / ".local" / "share" / "loop-engineering" / "worktrees"
        )

    def _recover_stale_run(self, store: RunStore, run_id: str) -> None:
        row = store.get_run(run_id)
        if row is None or row.get("state") != "stale":
            return
        repo = str(row["repo"])
        issue = int(str(row["issue"]))
        role = Role(str(row["role"]))
        result_path = self.paths.results / f"{run_id}.json"
        try:
            existing = json.loads(result_path.read_text(encoding="utf-8"))
            payload = existing if isinstance(existing, dict) else {}
        except (OSError, json.JSONDecodeError):
            payload = {}

        context: WorkspaceContext | None = None
        inspected: WorkspaceOutcome | None = None
        workspace_value = row.get("workspace_json")
        candidate_state = ""
        if isinstance(workspace_value, str) and workspace_value:
            recovery = json.loads(workspace_value)
            if not isinstance(recovery, dict):
                raise ValueError("invalid stale workspace context")
            context_value = recovery.get("context")
            intent_only = context_value is None
            if intent_only:
                context_value = recovery.get("intent")
            if not isinstance(context_value, dict):
                raise ValueError("invalid stale workspace context")
            context = _context_from_dict(context_value)
            candidate_state = str(recovery.get("candidate_state", ""))
            worker_status = str(payload.get("status", "failed"))
            if intent_only:
                final_status = "failed"
                exists = context.run_root.exists()
                payload.update(
                    workspace_path=str(context.path), source_sha=context.source_sha,
                    source_kind=context.source_kind, local_branch=context.local_branch,
                    remote_branch=context.remote_branch, pr_number=context.pr_number,
                    cleanup={
                        "outcome": "retained" if exists else "not-created",
                        "error": (
                            "workspace preparation was interrupted; partial workspace retained"
                            if exists else None
                        ),
                    },
                )
            else:
                candidate = Candidate(
                    repo, context.path, issue, "stale recovery", role,
                    candidate_state, "", self.now(),
                )
                verifier = None
                if worker_status == "completed":
                    if role is Role.PM:
                        verifier = lambda _sha, _changed: bool(
                            self.github.verify_pm_handoff(candidate, run_id)
                        )
                    elif role is Role.DEV:
                        verifier = lambda sha, changed: bool(
                            self.github.verify_dev_handoff(
                                candidate, context.remote_branch, sha, run_id,
                                has_changes=changed,
                            )
                        )
                    else:
                        source = ResolvedSource(
                            context.source_kind, context.source_sha, None,
                            context.remote_branch, context.pr_number,
                        )
                        verifier = lambda _sha, _changed: bool(
                            self.github.verify_qa_handoff(candidate, source, run_id)
                        )
                inspected = self.workspace_manager.inspect(
                    context.path, context, role, worker_status, verifier
                )
                final_status = (
                    "completed"
                    if worker_status == "completed" and inspected.failure is None
                    else "failed"
                )
                payload.update(
                    workspace_path=str(context.path), source_sha=context.source_sha,
                    source_kind=context.source_kind, final_sha=inspected.final_sha,
                    local_branch=context.local_branch, remote_branch=context.remote_branch,
                    pr_number=context.pr_number, clean=inspected.clean,
                    push_verified=inspected.push_verified,
                    handoff_verified=inspected.handoff_verified,
                    cleanup={"outcome": inspected.cleanup, "error": inspected.failure},
                )
        else:
            final_status = "failed"
            payload["cleanup"] = {"outcome": "not-created", "error": None}

        durable_status = (
            "cleanup-pending"
            if inspected is not None and inspected.cleanup == "pending"
            else final_status
        )
        payload.update(
            schema_version=1, run_id=run_id, status=durable_status,
            terminal_status=final_status,
            profile=str(row["profile"]), role=role.value,
            repository=repo, issue=issue,
        )
        if final_status == "failed" and "error" not in payload:
            payload.update(
                failure_stage="stale-recovery",
                error="run stopped without durable terminalization",
                exit_code=1,
            )
        result_path = ResultStore(self.paths.results).write(run_id, payload)
        journal_payload: dict[str, object] = {
            "schema_version": 1, "run_id": run_id, "repository": repo,
            "outcome": final_status, "result_path": str(result_path),
        }
        if context is not None and inspected is not None and inspected.cleanup == "pending":
            journal_payload["cleanup"] = {
                "context": _context_to_dict(context),
                "outcome": _outcome_to_dict(inspected),
            }
        journal = ResultStore(self.paths.terminalization).write(run_id, journal_payload)
        if context is not None and inspected is not None and inspected.cleanup == "pending":
            cleaned = self.workspace_manager.cleanup(context, inspected)
            payload["cleanup"] = {"outcome": cleaned.cleanup, "error": cleaned.failure}
            if cleaned.cleanup == "pending":
                payload.update(
                    status="failed", exit_code=1, failure_stage="cleanup",
                    error=cleaned.failure or "stale workspace cleanup remains pending",
                )
                ResultStore(self.paths.results).write(run_id, payload)
                raise RuntimeError("stale workspace cleanup remains pending")
        _canonicalize_terminal_payload(payload, final_status)
        result_path = ResultStore(self.paths.results).write(run_id, payload)
        store.finish(run_id, final_status, result_path)
        if not store.is_finished(run_id, final_status, result_path):
            raise RuntimeError("stale terminal run read-back did not match")
        journal.unlink()
        directory_fd = os.open(journal.parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def tick(self, *, dry_run: bool = False) -> TickResult:
        failures: list[RepositoryFailure] = []
        store: RunStore | None = None
        if not dry_run:
            self.paths.ensure()
            store = RunStore(self.paths.database)
            for journal_path in sorted(self.paths.terminalization.glob("*.json")):
                entry: object = None
                try:
                    entry = json.loads(journal_path.read_text(encoding="utf-8"))
                    if not isinstance(entry, dict) or entry.get("schema_version") != 1:
                        raise ValueError("invalid terminalization journal")
                    run_id = entry.get("run_id")
                    outcome = entry.get("outcome")
                    result_path_value = entry.get("result_path")
                    if not all(isinstance(value, str) and value for value in (
                        run_id, outcome, result_path_value
                    )):
                        raise ValueError("invalid terminalization journal")
                    assert isinstance(run_id, str)
                    assert isinstance(outcome, str)
                    assert isinstance(result_path_value, str)
                    result_path = Path(result_path_value)
                    expected_result_path = self.paths.results / f"{run_id}.json"
                    if result_path != expected_result_path or not result_path.is_file():
                        raise RuntimeError("terminalization result path is missing or invalid")
                    try:
                        result_payload = json.loads(result_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise RuntimeError("cannot read terminalization result") from exc
                    if not isinstance(result_payload, dict):
                        raise ValueError("invalid terminalization result")
                    cleanup_entry = entry.get("cleanup")
                    if cleanup_entry is not None:
                        if not isinstance(cleanup_entry, dict):
                            raise ValueError("invalid cleanup journal")
                        context_data = cleanup_entry.get("context")
                        outcome_data = cleanup_entry.get("outcome")
                        if not isinstance(context_data, dict) or not isinstance(outcome_data, dict):
                            raise ValueError("invalid cleanup journal")
                        context = _context_from_dict(context_data)
                        inspected = WorkspaceOutcome(
                            final_sha=outcome_data.get("final_sha"),
                            clean=outcome_data.get("clean") is True,
                            push_verified=outcome_data.get("push_verified") is True,
                            handoff_verified=outcome_data.get("handoff_verified") is True,
                            cleanup=str(outcome_data["cleanup"]),
                            failure=outcome_data.get("failure"),
                        )
                        cleaned = self.workspace_manager.cleanup(context, inspected)
                        result_payload["cleanup"] = {
                            "outcome": cleaned.cleanup,
                            "error": cleaned.failure,
                        }
                        if cleaned.cleanup == "pending":
                            result_payload.update(
                                status="failed", exit_code=1, failure_stage="cleanup",
                                error=cleaned.failure or "workspace cleanup remains pending",
                            )
                            ResultStore(self.paths.results).write(run_id, result_payload)
                            raise RuntimeError("workspace cleanup remains pending")
                    _canonicalize_terminal_payload(result_payload, outcome)
                    result_path = ResultStore(self.paths.results).write(run_id, result_payload)
                    recovery_error: Exception | None = None
                    for _attempt in range(2):
                        try:
                            store.finish(run_id, outcome, result_path)
                            if not store.is_finished(run_id, outcome, result_path):
                                raise RuntimeError(
                                    "terminal run read-back did not match journal"
                                )
                            recovery_error = None
                            break
                        except Exception as exc:
                            recovery_error = exc
                    if recovery_error is not None:
                        raise recovery_error
                    journal_path.unlink()
                    directory_fd = os.open(self.paths.terminalization, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except Exception as exc:
                    repository = (
                        str(entry.get("repository", "unknown"))
                        if isinstance(entry, dict)
                        else "unknown"
                    )
                    failures.append(RepositoryFailure(
                        repository, "terminalization-recovery", redact_text(str(exc))
                    ))
        candidates: list[Candidate] = []
        scan_groups: list[tuple[str, list[RepositoryConfig]]]
        if self.repositories and all(
            hasattr(repo, "slug") and hasattr(repo, "enabled")
            for repo in self.repositories
        ):
            scan_groups = [
                (repo.slug, [repo]) for repo in self.repositories if repo.enabled
            ]
        else:
            scan_groups = [("unknown", self.repositories)]
        for repository, group in scan_groups:
            try:
                candidates.extend(self.github.scan(group))
            except Exception as exc:
                failures.append(
                    RepositoryFailure(repository, "scan", redact_text(str(exc)))
                )
        candidates = list({item.identity: item for item in candidates}.values())

        supports_resolution = callable(getattr(self.github, "resolve_source", None))
        sources: dict[tuple[str, int], ResolvedSource] = {}
        healthy: list[Candidate] = []
        blocked_repositories: set[str] = set()
        trusted_origins: dict[str, str] = {}

        if not dry_run:
            for candidate in candidates:
                if candidate.repo in blocked_repositories:
                    continue
                try:
                    trusted_origins[candidate.repo] = validate_repository_origin(
                        candidate.repo_path, candidate.repo
                    )
                except Exception as exc:
                    blocked_repositories.add(candidate.repo)
                    failures.append(
                        RepositoryFailure(
                            candidate.repo, "origin", redact_text(str(exc))
                        )
                    )
            candidates = [
                item for item in candidates if item.repo not in blocked_repositories
            ]

        if supports_resolution:
            for candidate in candidates:
                try:
                    source = self.github.resolve_source(candidate)
                    if self.enforce_contracts and dry_run:
                        probe = Assignment(self.workers[0], candidate)
                        for relative in _required_contracts(probe):
                            expected = (_HARNESS_ROOT / relative).read_bytes()
                            actual = self.github.file_at_revision(
                                candidate.repo, relative, source.sha
                            )
                            if actual != expected:
                                raise ValueError(
                                    f"contract {relative} is not synchronized at assigned revision"
                                )
                    sources[candidate.identity] = source
                    healthy.append(candidate)
                except Exception as exc:
                    failures.append(
                        RepositoryFailure(
                            candidate.repo, "source-preflight", redact_text(str(exc))
                        )
                    )
            candidates = healthy
        elif self.enforce_contracts:
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
            return TickResult("dry-run", planned, [], [], failures)

        assert store is not None
        reservations: list[tuple[str, Assignment]] = []
        workspace_intents: dict[str, WorkspaceContext] = {}
        workspace_intent_errors: dict[str, Exception] = {}
        with FileLock(self.paths.claim_lock):
            reclaimed = store.reclaim_stale(self.now(), process_is_alive)
        blocked_stale_profiles: set[str] = set()
        blocked_stale_issues: set[tuple[str, int]] = set()
        for reclaimed_run_id in reclaimed:
            row = store.get_run(reclaimed_run_id)
            if row is None:
                continue
            profile = row.get("profile")
            repository = row.get("repo")
            issue = row.get("issue")
            if isinstance(profile, str) and profile:
                blocked_stale_profiles.add(profile)
            if isinstance(repository, str) and type(issue) is int:
                blocked_stale_issues.add((repository, issue))
        stale_ids = store.stale_run_ids() if callable(getattr(store, "stale_run_ids", None)) else []
        # Every stale row participates in the scan snapshot quarantine, whether
        # it became stale during this tick or predates it, and whether recovery
        # succeeds. Recovery may mutate authoritative state after scanning, so
        # only the next tick may redispatch that profile or repository issue.
        for stale_run_id in stale_ids:
            row = store.get_run(stale_run_id)
            if row is None:
                continue
            profile = row.get("profile")
            repository = row.get("repo")
            issue = row.get("issue")
            if isinstance(profile, str) and profile:
                blocked_stale_profiles.add(profile)
            if isinstance(repository, str) and type(issue) is int:
                blocked_stale_issues.add((repository, issue))
        for stale_run_id in stale_ids:
            try:
                self._recover_stale_run(store, stale_run_id)
            except Exception as exc:
                row = store.get_run(stale_run_id)
                repository = str(row.get("repo", "unknown")) if row else "unknown"
                if row is not None:
                    profile = row.get("profile")
                    issue = row.get("issue")
                    if isinstance(profile, str) and profile:
                        blocked_stale_profiles.add(profile)
                    if type(issue) is int:
                        blocked_stale_issues.add((repository, issue))
                failures.append(RepositoryFailure(
                    repository,
                    "stale-recovery", redact_text(str(exc)),
                ))
        with FileLock(self.paths.claim_lock):
            reserved_profiles: set[str] = set()
            reserved_issues: set[tuple[str, int]] = set()
            conflicted_issues: set[tuple[str, int]] = set()
            while True:
                planned = plan_assignments(
                    candidates,
                    self.workers,
                    busy_profiles=(
                        store.active_profiles() | reserved_profiles | blocked_stale_profiles
                    ),
                    active_issues=(
                        store.active_issues() | reserved_issues | conflicted_issues
                        | blocked_stale_issues
                    ),
                )
                if not planned:
                    break
                for item in planned:
                    run_id = f"run-{uuid.uuid4().hex}"
                    source = sources.get(item.candidate.identity)
                    intent = None
                    if source is not None:
                        try:
                            intent = self.workspace_manager.intent(
                                run_id,
                                item.candidate.repo_path,
                                trusted_origins[item.candidate.repo],
                                source,
                                item.candidate.role,
                                item.candidate.number,
                                repo_slug=item.candidate.repo,
                            )
                        except Exception as exc:
                            conflicted_issues.add(item.candidate.identity)
                            failures.append(RepositoryFailure(
                                item.candidate.repo, "workspace-intent", redact_text(str(exc))
                            ))
                            continue
                    if store.reserve(item, run_id, self.now()):
                        if intent is not None:
                            workspace_intents[run_id] = intent
                            try:
                                store.attach_workspace(run_id, {
                                    "intent": _context_to_dict(intent),
                                    "candidate_state": item.candidate.state,
                                })
                            except Exception as exc:
                                workspace_intent_errors[run_id] = exc
                        reservations.append((run_id, item))
                        reserved_profiles.add(item.worker.profile)
                        reserved_issues.add(item.candidate.identity)
                    else:
                        conflicted_issues.add(item.candidate.identity)

        def persist_failure(
            run_id: str,
            item: Assignment,
            stage: str,
            error: Exception | str,
            extra: dict[str, object] | None = None,
            *,
            status: str = "preflight-failed",
            exit_code: int | None = None,
        ) -> WorkerResult:
            payload: dict[str, object] = {
                "schema_version": 1,
                "run_id": run_id,
                "status": status,
                "exit_code": exit_code,
                "profile": item.worker.profile,
                "role": item.worker.role.value,
                "repository": item.candidate.repo,
                "issue": item.candidate.number,
                "failure_stage": stage,
                "error": redact_text(str(error)),
            }
            if extra:
                payload.update(extra)
            path = self.paths.results / f"{run_id}.json"
            persistence_error: Exception | None = None
            for _attempt in range(2):
                try:
                    path = ResultStore(self.paths.results).write(run_id, payload)
                    persistence_error = None
                    break
                except Exception as exc:
                    persistence_error = exc
                    payload["persistence_error"] = redact_text(str(exc))
            if persistence_error is not None or not path.is_file():
                raise RuntimeError("failed result could not be durably persisted") from persistence_error
            return WorkerResult(run_id, status, exit_code, path)

        contexts: dict[str, WorkspaceContext] = {}
        pending_cleanup: dict[str, tuple[WorkspaceContext, WorkspaceOutcome]] = {}

        def write_journal(
            run_id: str,
            item: Assignment,
            outcome: str,
            result_path: Path,
            cleanup_item: tuple[WorkspaceContext, WorkspaceOutcome] | None = None,
        ) -> Path:
            payload: dict[str, object] = {
                "schema_version": 1,
                "run_id": run_id,
                "repository": item.candidate.repo,
                "outcome": outcome,
                "result_path": str(result_path),
            }
            if cleanup_item is not None:
                context, inspected = cleanup_item
                payload["cleanup"] = {
                    "context": _context_to_dict(context),
                    "outcome": _outcome_to_dict(inspected),
                }
            return ResultStore(self.paths.terminalization).write(run_id, payload)

        def remove_journal(path: Path) -> None:
            path.unlink()
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        def execute_assignment(reservation: tuple[str, Assignment]) -> WorkerResult:
            run_id, original = reservation
            source = sources.get(original.candidate.identity)
            if run_id in workspace_intent_errors:
                return persist_failure(
                    run_id, original, "workspace-intent",
                    workspace_intent_errors[run_id],
                    {"cleanup": {"outcome": "not-created"}},
                    status="failed", exit_code=1,
                )
            if source is None:
                try:
                    result = self.worker_runner.run(
                        run_id,
                        original,
                        timeout=self.worker_timeout_seconds,
                        on_started=lambda pid, start: store.mark_running(
                            run_id, pid=pid, process_start=start, at=self.now()
                        ),
                        on_heartbeat=lambda: store.heartbeat(run_id, self.now()),
                    )
                except Exception as exc:
                    result = persist_failure(run_id, original, "launch", exc)
                return result

            context = None
            try:
                context = self.workspace_manager.prepare(
                    run_id,
                    original.candidate.repo_path,
                    trusted_origins[original.candidate.repo],
                    source,
                    original.candidate.role,
                    original.candidate.number,
                    repo_slug=original.candidate.repo,
                )
                contexts[run_id] = context
                store.attach_workspace(run_id, {
                    "context": _context_to_dict(context),
                    "candidate_state": original.candidate.state,
                })
                item = replace(original, workspace=context)
                if self.enforce_contracts:
                    validate_assignment_contracts(item)
                if self.github.resolve_source(original.candidate) != source:
                    raise RuntimeError("assigned source head moved during workspace preparation")
            except Exception as exc:
                extra: dict[str, object] = {
                    "source_sha": source.sha,
                    "source_kind": source.kind,
                    "remote_branch": source.remote_branch,
                    "pr_number": source.pr_number,
                    "cleanup": {"outcome": "not-created"},
                }
                if context is not None:
                    outcome = self.workspace_manager.inspect(
                        original.candidate.repo_path,
                        context,
                        original.candidate.role,
                        "preflight-failed",
                    )
                    extra.update(
                        workspace_path=str(context.path),
                        final_sha=outcome.final_sha,
                        cleanup={"outcome": outcome.cleanup, "error": outcome.failure},
                    )
                    if outcome.cleanup == "pending":
                        pending_cleanup[run_id] = (context, outcome)
                elif (intent := workspace_intents.get(run_id)) is not None:
                    exists = intent.run_root.exists()
                    extra.update(
                        workspace_path=str(intent.path),
                        cleanup={
                            "outcome": "retained" if exists else "not-created",
                            "error": (
                                "workspace preparation failed with partial evidence retained"
                                if exists else None
                            ),
                        },
                    )
                result = persist_failure(run_id, original, "workspace", exc, extra)
                return result

            observation: GitObjectObservation | None = None
            try:
                if original.candidate.role is Role.DEV:
                    starter = getattr(self.workspace_manager, "start_object_observation", None)
                    if callable(starter):
                        observation = cast(GitObjectObservation, starter(context))
                        context = replace(context, audit_path=observation.audit_path)
                        contexts[run_id] = context
                        item = replace(original, workspace=context)
                        store.attach_workspace(run_id, {
                            "context": _context_to_dict(context),
                            "candidate_state": original.candidate.state,
                        })
                result = self.worker_runner.run(
                    run_id,
                    item,
                    timeout=self.worker_timeout_seconds,
                    on_started=lambda pid, start: store.mark_running(
                        run_id, pid=pid, process_start=start, at=self.now()
                    ),
                    on_heartbeat=lambda: store.heartbeat(run_id, self.now()),
                )
            except Exception as exc:
                result = persist_failure(run_id, item, "launch", exc)
            finally:
                if observation is not None:
                    context = replace(context, **observation.stop())
                    contexts[run_id] = context
                    item = replace(original, workspace=context)
                    store.attach_workspace(run_id, {
                        "context": _context_to_dict(context),
                        "candidate_state": original.candidate.state,
                    })

            verifier = None
            if result.status == "completed" and original.candidate.role is Role.PM:
                verifier = lambda _sha, _has_changes: bool(
                    self.github.verify_pm_handoff(original.candidate, run_id)
                )
            elif result.status == "completed" and original.candidate.role is Role.DEV:
                verifier = lambda sha, has_changes: bool(
                    self.github.verify_dev_handoff(
                        original.candidate, context.remote_branch, sha, run_id,
                        has_changes=has_changes,
                    )
                )
            elif result.status == "completed" and original.candidate.role is Role.QA:
                verifier = lambda _sha, _has_changes: bool(
                    self.github.verify_qa_handoff(
                        original.candidate, source, run_id
                    )
                )
            outcome = self.workspace_manager.inspect(
                original.candidate.repo_path,
                context,
                original.candidate.role,
                result.status,
                verifier,
            )
            final_status = result.status
            postflight_failure = result.status == "completed" and outcome.failure is not None
            if postflight_failure:
                final_status = "failed"
            try:
                payload = json.loads(result.result_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {}
            except (OSError, json.JSONDecodeError):
                payload = {}
            sanitized_failure = redact_text(outcome.failure) if outcome.failure is not None else None
            durable_status = "cleanup-pending" if outcome.cleanup == "pending" else final_status
            payload.update(
                status=durable_status,
                terminal_status=final_status,
                workspace_path=str(context.path),
                source_sha=context.source_sha,
                final_sha=outcome.final_sha,
                source_kind=context.source_kind,
                local_branch=context.local_branch,
                remote_branch=context.remote_branch,
                pr_number=context.pr_number,
                observed_objects=list(context.observed_objects),
                audit_path=str(context.audit_path) if context.audit_path is not None else None,
                observation_error=context.observation_error,
                clean=outcome.clean,
                push_verified=outcome.push_verified,
                handoff_verified=outcome.handoff_verified,
                cleanup={"outcome": outcome.cleanup, "error": sanitized_failure},
            )
            if postflight_failure:
                payload.update(
                    exit_code=1,
                    failure_stage="postflight",
                    error=sanitized_failure or "postflight verification failed",
                )
            result_path = ResultStore(self.paths.results).write(run_id, payload)
            final_result = WorkerResult(
                run_id, final_status, 1 if postflight_failure else result.exit_code, result_path
            )
            if outcome.cleanup == "pending":
                pending_cleanup[run_id] = (context, outcome)
            return final_result

        def execute(reservation: tuple[str, Assignment]) -> WorkerResult:
            run_id, original = reservation
            try:
                result = execute_assignment(reservation)
            except Exception as exc:
                context = contexts.get(run_id)
                extra: dict[str, object] = {"cleanup": {"outcome": "retained"}}
                if context is not None:
                    extra.update(
                        workspace_path=str(context.path),
                        source_sha=context.source_sha,
                        source_kind=context.source_kind,
                        remote_branch=context.remote_branch,
                        pr_number=context.pr_number,
                    )
                result = persist_failure(
                    run_id, original, "postflight", exc, extra,
                    status="failed", exit_code=1,
                )

            cleanup_item = pending_cleanup.get(run_id)
            desired_outcome = result.status
            try:
                journal = write_journal(
                    run_id, original, desired_outcome, result.result_path, cleanup_item
                )
            except Exception as exc:
                result = persist_failure(
                    run_id, original, "terminalization-boundary", exc,
                    {"cleanup": {"outcome": "pending" if cleanup_item else "retained"}},
                    status="failed", exit_code=1,
                )
                try:
                    journal = write_journal(
                        run_id, original, "failed", result.result_path, cleanup_item
                    )
                except Exception:
                    journal = None
                desired_outcome = "failed"
            if journal is None:
                # Destructive cleanup is forbidden without a durable retry record.
                try:
                    store.finish(run_id, "failed", result.result_path)
                except Exception:
                    pass
                return WorkerResult(run_id, "failed", 1, result.result_path)

            if cleanup_item is not None:
                context, inspected = cleanup_item
                try:
                    cleaned = self.workspace_manager.cleanup(context, inspected)
                except Exception as exc:
                    cleaned = replace(
                        inspected, cleanup="pending",
                        failure=f"workspace cleanup failed: {redact_text(str(exc))}",
                    )
                try:
                    payload = json.loads(result.result_path.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict):
                        payload = {}
                    payload["cleanup"] = {
                        "outcome": cleaned.cleanup,
                        "error": cleaned.failure,
                    }
                    if cleaned.cleanup == "pending":
                        payload.update(
                            status="failed", exit_code=1, failure_stage="cleanup",
                            error=cleaned.failure or "workspace cleanup remains pending",
                        )
                    else:
                        _canonicalize_terminal_payload(payload, desired_outcome)
                    result_path = ResultStore(self.paths.results).write(run_id, payload)
                    result = WorkerResult(
                        result.run_id,
                        desired_outcome if cleaned.cleanup != "pending" else "failed",
                        result.exit_code if cleaned.cleanup != "pending" else 1,
                        result_path,
                    )
                except Exception:
                    # The durable cleanup-pending artifact and journal remain retryable.
                    return WorkerResult(result.run_id, "failed", 1, result.result_path)
                if cleaned.cleanup == "pending":
                    return result

            finish_error: Exception | None = None
            confirmed_active = False
            for _attempt in range(2):
                try:
                    store.finish(run_id, desired_outcome, result.result_path)
                    if not store.is_finished(run_id, desired_outcome, result.result_path):
                        raise RuntimeError("terminal run read-back did not match")
                    finish_error = None
                    break
                except Exception as exc:
                    finish_error = exc
                    try:
                        row = store.get_run(run_id)
                    except Exception:
                        row = None
                    else:
                        confirmed_active = row is not None and row.get("state") in {
                            "prepared", "running", "stale"
                        }
                    if row is not None and (
                        row.get("state"), row.get("outcome"), row.get("result_path")
                    ) == ("finished", desired_outcome, str(result.result_path)):
                        finish_error = None
                        break
            if finish_error is not None:
                if not confirmed_active:
                    # The finish may have committed even though acknowledgement and
                    # read-back both failed. Keep the desired tuple and artifact for
                    # idempotent recovery rather than inventing a conflicting failure.
                    return WorkerResult(run_id, "failed", 1, result.result_path)
                try:
                    journal = write_journal(
                        run_id, original, "failed", result.result_path, None
                    )
                    result = persist_failure(
                        run_id, original, "persistence", finish_error,
                        {"cleanup": {"outcome": "deleted" if cleanup_item else "retained"}},
                        status="failed", exit_code=1,
                    )
                    journal = write_journal(
                        run_id, original, "failed", result.result_path, None
                    )
                    store.finish(run_id, "failed", result.result_path)
                    if not store.is_finished(run_id, "failed", result.result_path):
                        raise RuntimeError("failed terminal run read-back did not match")
                except Exception:
                    return WorkerResult(run_id, "failed", 1, result.result_path)
            remove_journal(journal)
            return result

        results = run_concurrently(reservations, execute)
        return TickResult(
            "live", [item for _, item in reservations], results, reclaimed, failures
        )
