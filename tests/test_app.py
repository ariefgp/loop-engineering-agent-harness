from __future__ import annotations

import os
import json
import sqlite3
import unittest
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from loop_harness.app import (
    Harness, _context_to_dict, _outcome_to_dict, validate_repository_origin,
)
from loop_harness.models import Candidate, ResolvedSource, Role, WorkspaceContext
from loop_harness.runtime import FileLock, ResultStore, RuntimePaths
from loop_harness.scheduler import Assignment, DEFAULT_WORKERS, WorkerSpec
from loop_harness.store import RunStore
from loop_harness.worker import WorkerResult
from loop_harness.workspace import WorkspaceManager, WorkspaceOutcome


NOW = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)


class FakeGitHub:
    def __init__(self, candidates):
        self.candidates = candidates
        self.scans = 0

    def scan(self, repositories):
        self.scans += 1
        return list(self.candidates)


class ExplodingRunner:
    def run(self, *args, **kwargs):
        raise AssertionError("dry-run must not launch a worker")


class HarnessTests(unittest.TestCase):
    def test_completed_worker_postflight_failures_are_canonical_failed_results(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            pm_repo = root / "pm-repo"
            dev_repo = root / "dev-repo"
            pm_repo.mkdir()
            dev_repo.mkdir()
            pm = Candidate(
                "Example/pm", pm_repo, 1, "plan", Role.PM,
                "to be planned", "P0", NOW,
            )
            dev = Candidate(
                "Example/dev", dev_repo, 2, "feature", Role.DEV,
                "todo", "P0", NOW,
            )
            sources = {
                pm.identity: ResolvedSource("default", "a" * 40, "main", None, None),
                dev.identity: ResolvedSource("default", "b" * 40, "main", None, None),
            }

            class PostflightGitHub(FakeGitHub):
                def resolve_source(inner_self, candidate):
                    return sources[candidate.identity]

                def verify_pm_handoff(inner_self, _candidate, _run_id):
                    return False

                def verify_dev_handoff(inner_self, *_args, **_kwargs):
                    raise AssertionError("publication failure must short-circuit handoff")

            class CompletingRunner:
                def run(inner_self, run_id, assignment, **kwargs):
                    kwargs["on_started"](os.getpid(), f"identity-{run_id}")
                    path = ResultStore(runtime.results).write(run_id, {
                        "status": "completed", "exit_code": 0,
                        "issue": assignment.candidate.number,
                    })
                    return WorkerResult(run_id, "completed", 0, path)

            class FailingPostflightManager:
                def intent(inner_self, run_id, repository, origin, source, role, issue, **_kwargs):
                    run_root = root / "workspaces" / run_id
                    return WorkspaceContext(
                        run_root / "worktree", source.sha, source.kind,
                        f"loop-harness/{run_id}" if role is Role.DEV else None,
                        source.remote_branch, source.pr_number,
                        f"refs/loop-harness/{run_id}/source", run_root,
                        run_root / "git", origin,
                    )

                def prepare(inner_self, run_id, repository, origin, source, role, issue, **kwargs):
                    context = inner_self.intent(
                        run_id, repository, origin, source, role, issue, **kwargs
                    )
                    context.path.mkdir(parents=True)
                    context.git_dir.mkdir()
                    return context

                def inspect(inner_self, repository, context, role, status, verifier=None):
                    self.assertEqual("completed", status)
                    if role is Role.PM:
                        self.assertIsNotNone(verifier)
                        self.assertFalse(verifier(context.source_sha, False))
                        reason = "GitHub handoff does not match the terminal workspace state"
                        return WorkspaceOutcome(
                            context.source_sha, True, False, False, "retained", reason
                        )
                    reason = "remote publication verification failed: token=super-secret rejected"
                    return WorkspaceOutcome(
                        context.source_sha, True, False, False, "retained", reason
                    )

            with patch(
                "loop_harness.app.validate_repository_origin",
                side_effect=lambda _path, slug: f"https://github.com/{slug}.git",
            ):
                tick = Harness(
                    github=PostflightGitHub([pm, dev]), repositories=[], paths=runtime,
                    workers=(DEFAULT_WORKERS[0], DEFAULT_WORKERS[1]),
                    worker_runner=CompletingRunner(),
                    workspace_manager=FailingPostflightManager(), now=lambda: NOW,
                    enforce_contracts=False,
                ).tick()

            self.assertEqual(2, len(tick.results))
            for result in tick.results:
                with self.subTest(run_id=result.run_id):
                    self.assertEqual("failed", result.status)
                    self.assertEqual(1, result.exit_code)
                    payload = json.loads(result.result_path.read_text(encoding="utf-8"))
                    self.assertEqual("failed", payload["status"])
                    self.assertEqual("failed", payload["terminal_status"])
                    self.assertEqual(1, payload["exit_code"])
                    self.assertEqual("postflight", payload["failure_stage"])
                    self.assertIsInstance(payload["error"], str)
                    self.assertTrue(payload["error"])
                    self.assertEqual(payload["cleanup"]["error"], payload["error"])
                    self.assertNotIn("super-secret", json.dumps(payload))

    def test_cleanup_journal_recovery_converges_completed_payload_when_paths_are_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            runtime.ensure()
            repo = root / "repo"
            repo.mkdir()
            candidate = Candidate(
                "Example/project", repo, 12, "plan", Role.PM,
                "to be planned", "P0", NOW,
            )
            worker = DEFAULT_WORKERS[0]
            run_id = "run-cleanup-journal"
            store = RunStore(runtime.database)
            self.assertTrue(store.reserve(Assignment(worker, candidate), run_id, NOW))
            manager = WorkspaceManager(runtime.root / "workspaces")
            context = manager.intent(
                run_id, repo, "https://github.com/Example/project.git",
                ResolvedSource("default", "a" * 40, "main", None, None),
                Role.PM, candidate.number, repo_slug=candidate.repo,
            )
            context = replace(
                context,
                audit_path=manager.root / ".audit" / context.run_root.name,
            )
            inspected = WorkspaceOutcome(
                context.source_sha, True, False, True, "pending",
                "workspace cleanup failed: interrupted",
            )
            result_path = ResultStore(runtime.results).write(run_id, {
                "schema_version": 1,
                "run_id": run_id,
                "status": "failed",
                "terminal_status": "completed",
                "exit_code": 1,
                "profile": worker.profile,
                "role": worker.role.value,
                "repository": candidate.repo,
                "issue": candidate.number,
                "failure_stage": "cleanup",
                "error": "workspace cleanup failed: interrupted",
                "cleanup": {"outcome": "pending", "error": "interrupted"},
            })
            ResultStore(runtime.terminalization).write(run_id, {
                "schema_version": 1,
                "run_id": run_id,
                "repository": candidate.repo,
                "outcome": "completed",
                "result_path": str(result_path),
                "cleanup": {
                    "context": _context_to_dict(context),
                    "outcome": _outcome_to_dict(inspected),
                },
            })

            tick = Harness(
                github=FakeGitHub([]), repositories=[], paths=runtime,
                workers=(worker,), worker_runner=ExplodingRunner(),
                workspace_manager=manager,
                now=lambda: NOW, enforce_contracts=False,
            ).tick()

            self.assertEqual([], tick.failures)
            self.assertEqual({
                "schema_version": 1,
                "run_id": run_id,
                "status": "completed",
                "terminal_status": "completed",
                "exit_code": 0,
                "profile": worker.profile,
                "role": worker.role.value,
                "repository": candidate.repo,
                "issue": candidate.number,
                "cleanup": {"outcome": "deleted", "error": None},
            }, json.loads(result_path.read_text(encoding="utf-8")))
            self.assertEqual([], list(runtime.terminalization.glob("*.json")))
            with sqlite3.connect(runtime.database) as connection:
                self.assertEqual(
                    ("finished", "completed", str(result_path)),
                    connection.execute(
                        "SELECT state, outcome, result_path FROM runs WHERE run_id=?",
                        (run_id,),
                    ).fetchone(),
                )

    def test_crash_after_pruned_object_capture_recovers_durable_audit_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            remote = root / "remote.git"
            seed = root / "seed"
            canonical = root / "canonical"
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
            for key, value in (("user.email", "test@example.com"), ("user.name", "Test")):
                subprocess.run(["git", "-C", str(seed), "config", key, value], check=True)
            (seed / "README.md").write_text("source\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "source"], check=True)
            subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True)
            subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)
            subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
            subprocess.run(["git", "clone", "-q", str(remote), str(canonical)], check=True)
            source_sha = subprocess.run(
                ["git", "-C", str(canonical), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            candidate = Candidate(
                "Example/project", canonical, 12, "work", Role.DEV,
                "todo", "P0", NOW,
            )
            source = ResolvedSource("default", source_sha, "main", None, None)
            marker = root / "abandoned-sha"

            def clone_repository(_slug: str, destination: Path) -> None:
                subprocess.run(
                    ["git", "clone", "--bare", "-q", str(remote), str(destination)],
                    check=True,
                )

            manager = WorkspaceManager(
                root / "workspaces", clone_repository=clone_repository,
                remote_head=lambda _slug, _branch: None,
                default_repo_slug="Example/project",
            )

            class SourceGitHub(FakeGitHub):
                def resolve_source(inner_self, _candidate):
                    return source

            class CrashingRunner:
                def run(inner_self, _run_id, assignment, **_kwargs):
                    context = assignment.workspace
                    assert context is not None and context.audit_path is not None
                    with sqlite3.connect(runtime.database) as connection:
                        attached = json.loads(
                            connection.execute("SELECT workspace_json FROM runs").fetchone()[0]
                        )["context"]
                    assert attached["audit_path"] == str(context.audit_path)
                    for key, value in (("user.email", "test@example.com"), ("user.name", "Test")):
                        subprocess.run(
                            ["git", "-C", str(context.path), "config", key, value], check=True
                        )
                    (context.path / "lost.txt").write_text("lost\n", encoding="utf-8")
                    subprocess.run(["git", "-C", str(context.path), "add", "lost.txt"], check=True)
                    assigned_ref = context.git_dir.joinpath(
                        "refs", "heads", *context.local_branch.split("/")
                    )
                    assigned_reflog = context.git_dir.joinpath(
                        "logs", "refs", "heads", *context.local_branch.split("/")
                    )
                    subprocess.run(
                        [
                            "strace", "--follow-forks", "--decode-fds=path",
                            "--string-limit=65535",
                            "--trace=write,writev,pwrite64,openat,close,rename,renameat,renameat2,unlink,unlinkat",
                            "--output", str(context.audit_path / "assigned-ref.trace"),
                            *[
                                value
                                for path in (
                                    assigned_ref, Path(str(assigned_ref) + ".lock"),
                                    assigned_reflog, Path(str(assigned_reflog) + ".lock"),
                                )
                                for value in ("--trace-path", str(path))
                            ],
                            "--", "git", "-C", str(context.path),
                            "commit", "-q", "-m", "abandoned",
                        ],
                        check=True,
                    )
                    abandoned = subprocess.run(
                        ["git", "-C", str(context.path), "rev-parse", "HEAD"],
                        text=True, capture_output=True, check=True,
                    ).stdout.strip()
                    marker.write_text(abandoned, encoding="ascii")
                    subprocess.run(
                        ["git", "-C", str(context.path), "reset", "--hard", source_sha], check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        ["git", "-C", str(context.path), "reflog", "expire", "--expire=now", "--all"],
                        check=True,
                    )
                    subprocess.run(
                        ["git", "-C", str(context.path), "gc", "--prune=now", "--quiet"], check=True
                    )
                    os._exit(73)

            child = os.fork()
            if child == 0:
                with patch("loop_harness.app.validate_repository_origin", return_value=str(remote)):
                    Harness(
                        github=SourceGitHub([candidate]), repositories=[], paths=runtime,
                        workers=(DEFAULT_WORKERS[1],), worker_runner=CrashingRunner(),
                        workspace_manager=manager, now=lambda: NOW, enforce_contracts=False,
                    ).tick()
                os._exit(74)
            _pid, status = os.waitpid(child, 0)
            self.assertEqual(73, os.waitstatus_to_exitcode(status))

            recovered = Harness(
                github=FakeGitHub([]), repositories=[], paths=runtime,
                workers=(DEFAULT_WORKERS[1],), worker_runner=ExplodingRunner(),
                workspace_manager=manager, now=lambda: NOW + timedelta(hours=1),
                enforce_contracts=False,
            ).tick()

            self.assertEqual(1, len(recovered.reclaimed))
            payload = json.loads((runtime.results / f"{recovered.reclaimed[0]}.json").read_text())
            self.assertEqual("failed", payload["status"])
            self.assertEqual("retained", payload["cleanup"]["outcome"])
            self.assertIn("abandoned", payload["cleanup"]["error"])
            abandoned_sha = marker.read_text(encoding="ascii")
            workspace = json.loads(RunStore(runtime.database).get_run(recovered.reclaimed[0])["workspace_json"])
            self.assertIn(
                abandoned_sha,
                (Path(workspace["context"]["audit_path"]) / "assigned-ref.trace").read_text(),
            )

    def test_prepare_failure_keeps_preparation_intent_and_partial_root_tracked(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            repo = root / "repo"
            repo.mkdir()
            candidate = Candidate(
                "Example/project", repo, 9, "plan", Role.PM,
                "to be planned", "P0", NOW,
            )
            source = ResolvedSource("default", "a" * 40, "main", None, None)

            class SourceGitHub(FakeGitHub):
                def resolve_source(inner_self, _candidate):
                    return source

            class FailingPrepareManager:
                def intent(inner_self, run_id, repository, origin, resolved, role, issue, **_kwargs):
                    run_root = root / "workspaces" / run_id
                    return WorkspaceContext(
                        run_root / "worktree", resolved.sha, resolved.kind, None,
                        resolved.remote_branch, resolved.pr_number,
                        f"refs/loop-harness/{run_id}/source", run_root,
                        run_root / "git", origin,
                    )

                def prepare(inner_self, run_id, *_args, **_kwargs):
                    with sqlite3.connect(runtime.database) as connection:
                        raw = connection.execute(
                            "SELECT workspace_json FROM runs WHERE run_id=?", (run_id,)
                        ).fetchone()[0]
                    intent = json.loads(raw)["intent"]
                    run_root = Path(intent["run_root"])
                    run_root.mkdir(parents=True)
                    (run_root / "partial").write_text("evidence", encoding="ascii")
                    raise RuntimeError("injected prepare fault")

            with patch(
                "loop_harness.app.validate_repository_origin",
                return_value="https://github.com/Example/project.git",
            ):
                tick = Harness(
                    github=SourceGitHub([candidate]), repositories=[], paths=runtime,
                    workers=(DEFAULT_WORKERS[0],), worker_runner=ExplodingRunner(),
                    workspace_manager=FailingPrepareManager(), now=lambda: NOW,
                    enforce_contracts=False,
                ).tick()

            self.assertNotEqual("completed", tick.results[0].status)
            payload = json.loads(tick.results[0].result_path.read_text(encoding="utf-8"))
            self.assertEqual("retained", payload["cleanup"]["outcome"])
            self.assertTrue(Path(payload["workspace_path"]).parent.exists())
            with sqlite3.connect(runtime.database) as connection:
                workspace = json.loads(
                    connection.execute("SELECT workspace_json FROM runs").fetchone()[0]
                )
            self.assertEqual(payload["workspace_path"], workspace["intent"]["path"])

    def test_stale_recovery_retains_workspace_created_before_context_attachment(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            repo = root / "repo"
            repo.mkdir()
            candidate = Candidate(
                "Example/project", repo, 9, "plan", Role.PM,
                "to be planned", "P0", NOW,
            )
            source = ResolvedSource("default", "a" * 40, "main", None, None)
            manager = WorkspaceManager(
                root / "workspaces", default_repo_slug="Example/project"
            )
            intent = manager.intent(
                "run-interrupted", repo, "https://github.com/Example/project.git",
                source, Role.PM, 9,
            )
            intent.run_root.mkdir(parents=True)
            (intent.run_root / "prepared-evidence").write_text("keep", encoding="ascii")
            store = RunStore(runtime.database)
            self.assertTrue(store.reserve(Assignment(DEFAULT_WORKERS[0], candidate), "run-interrupted", NOW))
            store.attach_workspace("run-interrupted", {
                "intent": {
                    "path": str(intent.path), "source_sha": intent.source_sha,
                    "source_kind": intent.source_kind, "local_branch": intent.local_branch,
                    "remote_branch": intent.remote_branch, "pr_number": intent.pr_number,
                    "private_ref": intent.private_ref, "run_root": str(intent.run_root),
                    "git_dir": str(intent.git_dir),
                    "expected_origin_url": intent.expected_origin_url,
                    "baseline_commits": [], "repo_slug": intent.repo_slug,
                },
                "candidate_state": candidate.state,
            })

            tick = Harness(
                github=FakeGitHub([]), repositories=[], paths=runtime,
                workers=(DEFAULT_WORKERS[0],), worker_runner=ExplodingRunner(),
                workspace_manager=manager, now=lambda: NOW + timedelta(hours=1),
                enforce_contracts=False,
            ).tick()

            self.assertEqual(["run-interrupted"], tick.reclaimed)
            self.assertTrue(intent.run_root.exists())
            payload = json.loads((runtime.results / "run-interrupted.json").read_text())
            self.assertEqual(str(intent.path), payload["workspace_path"])
            self.assertEqual("retained", payload["cleanup"]["outcome"])
            with sqlite3.connect(runtime.database) as connection:
                self.assertEqual(
                    ("finished", "failed"),
                    connection.execute("SELECT state, outcome FROM runs").fetchone(),
                )

    def test_failed_stale_reconciliation_quarantines_profile_and_issue_for_tick(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            bad_repo = root / "bad"
            healthy_repo = root / "healthy"
            bad_repo.mkdir()
            healthy_repo.mkdir()
            bad = Candidate(
                "Example/bad", bad_repo, 9, "stale", Role.PM,
                "to be planned", "P0", NOW,
            )
            healthy = Candidate(
                "Example/healthy", healthy_repo, 10, "healthy", Role.PM,
                "to be planned", "P1", NOW + timedelta(minutes=1),
            )
            primary = DEFAULT_WORKERS[0]
            backup = WorkerSpec("backup-pm", Role.PM, ".agents/agent-pm.md")
            store = RunStore(runtime.database)
            self.assertTrue(store.reserve(Assignment(primary, bad), "run-malformed-stale", NOW))
            with sqlite3.connect(runtime.database) as connection:
                connection.execute(
                    "UPDATE runs SET workspace_json=? WHERE run_id=?",
                    ("{malformed", "run-malformed-stale"),
                )

            launches: list[tuple[str, int]] = []

            class CompletingRunner:
                def run(inner_self, run_id, assignment, **_kwargs):
                    launches.append((assignment.worker.profile, assignment.candidate.number))
                    result_path = runtime.results / f"{run_id}.json"
                    result_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
                    return WorkerResult(run_id, "completed", 0, result_path)

            with patch(
                "loop_harness.app.validate_repository_origin",
                side_effect=lambda _path, slug: f"https://github.com/{slug}.git",
            ):
                tick = Harness(
                    github=FakeGitHub([bad, healthy]), repositories=[], paths=runtime,
                    workers=(primary, backup), worker_runner=CompletingRunner(),
                    now=lambda: NOW + timedelta(hours=1), enforce_contracts=False,
                ).tick()

            self.assertEqual(["run-malformed-stale"], tick.reclaimed)
            self.assertEqual([("backup-pm", 10)], launches)
            self.assertEqual([10], [item.candidate.number for item in tick.assignments])
            self.assertTrue(any(failure.stage == "stale-recovery" for failure in tick.failures))
            with sqlite3.connect(runtime.database) as connection:
                stale = connection.execute(
                    "SELECT state, profile, repo, issue FROM runs WHERE run_id=?",
                    ("run-malformed-stale",),
                ).fetchone()
            self.assertEqual(("stale", primary.profile, bad.repo, bad.number), stale)

    def test_successful_stale_recovery_quarantines_original_claim_until_next_scan(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            stale_repo = root / "stale"
            healthy_repo = root / "healthy"
            stale_repo.mkdir()
            healthy_repo.mkdir()
            stale = Candidate(
                "Example/stale", stale_repo, 9, "stale", Role.PM,
                "to be planned", "P0", NOW,
            )
            healthy = Candidate(
                "Example/healthy", healthy_repo, 10, "healthy", Role.PM,
                "to be planned", "P1", NOW + timedelta(minutes=1),
            )
            primary = DEFAULT_WORKERS[0]
            backup = WorkerSpec("backup-pm", Role.PM, ".agents/agent-pm.md")
            store = RunStore(runtime.database)
            self.assertTrue(store.reserve(Assignment(primary, stale), "run-stale-success", NOW))
            recovery_context = WorkspaceContext(
                path=root / "recovery" / "worktree", source_sha="a" * 40,
                source_kind="default", local_branch=None, remote_branch=None,
                pr_number=None, private_ref="refs/loop-harness/run-stale-success/source",
                run_root=root / "recovery", git_dir=root / "recovery" / "git",
                expected_origin_url="https://github.com/Example/stale.git",
                repo_slug="Example/stale",
            )
            store.attach_workspace("run-stale-success", {
                "context": {
                    "path": str(recovery_context.path),
                    "source_sha": recovery_context.source_sha,
                    "source_kind": recovery_context.source_kind,
                    "local_branch": None, "remote_branch": None, "pr_number": None,
                    "private_ref": recovery_context.private_ref,
                    "run_root": str(recovery_context.run_root),
                    "git_dir": str(recovery_context.git_dir),
                    "expected_origin_url": recovery_context.expected_origin_url,
                    "baseline_commits": [], "repo_slug": recovery_context.repo_slug,
                    "observed_objects": [], "audit_path": None,
                    "observation_error": None,
                },
                "candidate_state": stale.state,
            })
            ResultStore(runtime.results).write(
                "run-stale-success", {"status": "completed"}
            )
            # Reproduce a stale row that predates this tick: reclaim_stale() will
            # not return it, but recovery must still quarantine its claim from
            # the candidate snapshot already read above.
            with sqlite3.connect(runtime.database) as connection:
                connection.execute(
                    "UPDATE runs SET state='stale', outcome='stale' WHERE run_id=?",
                    ("run-stale-success",),
                )

            class LifecycleGitHub(FakeGitHub):
                transitioned = False

                def scan(inner_self, repositories):
                    inner_self.scans += 1
                    return [stale, healthy] if not inner_self.transitioned else [healthy]

                def verify_pm_handoff(inner_self, _candidate, _run_id):
                    inner_self.transitioned = True
                    return True

            class RecoveryManager:
                def inspect(inner_self, _repository, context, _role, _status, verifier):
                    assert verifier is not None and verifier(context.source_sha, False)
                    return WorkspaceOutcome(
                        context.source_sha, True, False, True, "pending", None
                    )

                def cleanup(inner_self, _context, inspected):
                    return WorkspaceOutcome(
                        inspected.final_sha, inspected.clean, inspected.push_verified,
                        inspected.handoff_verified, "deleted", inspected.failure,
                    )

            launches: list[tuple[str, int]] = []

            class CompletingRunner:
                def run(inner_self, run_id, assignment, **_kwargs):
                    launches.append((assignment.worker.profile, assignment.candidate.number))
                    path = ResultStore(runtime.results).write(run_id, {"status": "completed"})
                    return WorkerResult(run_id, "completed", 0, path)

            github = LifecycleGitHub([])
            with patch(
                "loop_harness.app.validate_repository_origin",
                side_effect=lambda _path, slug: f"https://github.com/{slug}.git",
            ):
                first = Harness(
                    github=github, repositories=[], paths=runtime,
                    workers=(primary, backup), worker_runner=CompletingRunner(),
                    workspace_manager=RecoveryManager(),
                    now=lambda: NOW + timedelta(hours=1), enforce_contracts=False,
                ).tick()
                second = Harness(
                    github=github, repositories=[], paths=runtime,
                    workers=(primary, backup), worker_runner=CompletingRunner(),
                    workspace_manager=RecoveryManager(),
                    now=lambda: NOW + timedelta(hours=1), enforce_contracts=False,
                ).tick()

            self.assertEqual([], first.reclaimed)
            self.assertEqual([("backup-pm", 10), (primary.profile, 10)], launches)
            self.assertEqual([10], [item.candidate.number for item in first.assignments])
            self.assertEqual([10], [item.candidate.number for item in second.assignments])
            self.assertEqual(2, github.scans)
            self.assertTrue(github.transitioned)

    def test_journal_write_failure_before_finish_returns_durable_failed_result(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            candidate = Candidate(
                "Example/project", root, 9, "plan", Role.PM,
                "to be planned", "P0", NOW,
            )

            class CompletingRunner:
                def run(inner_self, run_id, assignment, **kwargs):
                    path = runtime.results / f"{run_id}.json"
                    path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
                    return WorkerResult(run_id, "completed", 0, path)

            original_write = ResultStore.write
            failed_once = False

            def fail_first_journal(store, run_id, payload):
                nonlocal failed_once
                if store.directory == runtime.terminalization and not failed_once:
                    failed_once = True
                    raise OSError("injected journal failure")
                return original_write(store, run_id, payload)

            with patch("loop_harness.app.validate_repository_origin"), patch.object(
                ResultStore, "write", autospec=True, side_effect=fail_first_journal
            ):
                tick = Harness(
                    github=FakeGitHub([candidate]), repositories=[], paths=runtime,
                    workers=(DEFAULT_WORKERS[0],), worker_runner=CompletingRunner(),
                    now=lambda: NOW, enforce_contracts=False,
                ).tick()

            self.assertEqual(1, len(tick.results))
            self.assertEqual("failed", tick.results[0].status)
            payload = json.loads(tick.results[0].result_path.read_text(encoding="utf-8"))
            self.assertEqual("terminalization-boundary", payload["failure_stage"])
            with sqlite3.connect(runtime.database) as connection:
                self.assertEqual(
                    ("finished", "failed"),
                    connection.execute("SELECT state, outcome FROM runs").fetchone(),
                )

    def test_postflight_failure_is_isolated_and_all_reservations_finish(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            pm_repo = root / "pm-repo"
            qa_repo = root / "qa-repo"
            pm_repo.mkdir()
            qa_repo.mkdir()
            pm = Candidate(
                "Example/pm", pm_repo, 1, "plan", Role.PM,
                "to be planned", "P0", NOW,
            )
            qa = Candidate(
                "Example/qa", qa_repo, 2, "review", Role.QA,
                "qa ready", "P0", NOW,
            )
            sources = {
                pm.identity: ResolvedSource("default", "a" * 40, "main", None, None),
                qa.identity: ResolvedSource("pr", "b" * 40, None, "feature/2", 22),
            }
            verified: list[tuple[str, str]] = []

            class HandoffGitHub(FakeGitHub):
                def resolve_source(inner_self, candidate):
                    return sources[candidate.identity]

                def verify_pm_handoff(inner_self, candidate, run_id):
                    verified.append((candidate.repo, run_id))
                    return True

                def verify_qa_handoff(inner_self, candidate, source, run_id):
                    self.assertEqual(sources[candidate.identity], source)
                    verified.append((candidate.repo, run_id))
                    return True

            class CompletingRunner:
                def run(inner_self, run_id, assignment, **kwargs):
                    kwargs["on_started"](os.getpid(), f"identity-{run_id}")
                    result_path = runtime.results / f"{run_id}.json"
                    result_path.write_text(
                        json.dumps({
                            "status": "completed", "stdout": "untrusted",
                            "issue": assignment.candidate.number,
                        }),
                        encoding="utf-8",
                    )
                    return WorkerResult(run_id, "completed", 0, result_path)

            class PartlyExplodingWorkspaceManager:
                def __init__(inner_self):
                    inner_self.contexts: dict[str, WorkspaceContext] = {}

                def intent(inner_self, run_id, repository, origin, source, role, issue, **_kwargs):
                    run_root = root / "workspaces" / run_id
                    return WorkspaceContext(
                        run_root / "worktree", source.sha, source.kind,
                        f"loop-harness/{run_id}" if role is Role.DEV else None,
                        source.remote_branch, source.pr_number,
                        f"refs/loop-harness/{run_id}/source", run_root,
                        run_root / "git", origin,
                    )

                def prepare(inner_self, run_id, repository, origin, source, role, issue, **_kwargs):
                    run_root = root / "workspaces" / run_id
                    path = run_root / "worktree"
                    git_dir = run_root / "git"
                    path.mkdir(parents=True)
                    git_dir.mkdir()
                    context = WorkspaceContext(
                        path, source.sha, source.kind,
                        f"loop-harness/{run_id}" if role is Role.DEV else None,
                        source.remote_branch, source.pr_number,
                        f"refs/loop-harness/{run_id}/source", run_root, git_dir, origin,
                    )
                    inner_self.contexts[repository.name] = context
                    return context

                def inspect(inner_self, repository, context, role, status, verifier=None):
                    self.assertEqual("completed", status)
                    self.assertIsNotNone(verifier)
                    self.assertTrue(verifier(context.source_sha, False))
                    if repository == pm_repo:
                        raise RuntimeError("postflight token=super-secret exploded")
                    return WorkspaceOutcome(
                        context.source_sha, True, False, True, "pending", None
                    )

                def cleanup(inner_self, context, inspected):
                    with sqlite3.connect(runtime.database) as connection:
                        row = connection.execute(
                            "SELECT state FROM runs WHERE issue=2"
                        ).fetchone()
                    self.assertIn(row, (("prepared",), ("running",)))
                    persisted = json.loads(
                        (runtime.results / f"{context.run_root.name}.json").read_text()
                    )
                    self.assertEqual("pending", persisted["cleanup"]["outcome"])
                    import shutil
                    if context.run_root.exists():
                        shutil.rmtree(context.run_root)
                    return WorkspaceOutcome(
                        context.source_sha, True, False, True, "deleted", None
                    )

            manager = PartlyExplodingWorkspaceManager()
            original_write = ResultStore.write

            def fail_post_delete_update(store, run_id, payload):
                cleanup = payload.get("cleanup", {})
                if isinstance(cleanup, dict) and cleanup.get("outcome") == "deleted":
                    raise OSError("injected post-delete persistence failure")
                return original_write(store, run_id, payload)

            with patch(
                "loop_harness.app.validate_repository_origin",
                side_effect=lambda path, slug: f"https://github.com/{slug}.git",
            ), patch.object(ResultStore, "write", autospec=True, side_effect=fail_post_delete_update):
                tick = Harness(
                    github=HandoffGitHub([pm, qa]), repositories=[], paths=runtime,
                    workers=(DEFAULT_WORKERS[0], DEFAULT_WORKERS[4]),
                    worker_runner=CompletingRunner(), workspace_manager=manager,
                    now=lambda: NOW, enforce_contracts=False,
                ).tick()

            by_issue = {
                json.loads(result.result_path.read_text())["issue"]: result
                for result in tick.results
            }
            self.assertEqual({1, 2}, set(by_issue))
            self.assertEqual("failed", by_issue[1].status)
            self.assertEqual(1, by_issue[1].exit_code)
            self.assertEqual("failed", by_issue[2].status)
            self.assertNotEqual(0, by_issue[2].exit_code)
            failed = json.loads(by_issue[1].result_path.read_text())
            self.assertEqual("postflight", failed["failure_stage"])
            self.assertEqual("retained", failed["cleanup"]["outcome"])
            self.assertNotIn("super-secret", json.dumps(failed))
            self.assertIn("[REDACTED]", failed["error"])
            self.assertTrue(manager.contexts[pm_repo.name].path.exists())
            self.assertFalse(manager.contexts[qa_repo.name].path.exists())
            qa_payload = json.loads(by_issue[2].result_path.read_text())
            self.assertEqual("cleanup-pending", qa_payload["status"])
            self.assertEqual("pending", qa_payload["cleanup"]["outcome"])
            self.assertEqual(1, len(list(runtime.terminalization.glob("*.json"))))
            with sqlite3.connect(runtime.database) as connection:
                self.assertIn(
                    connection.execute("SELECT state FROM runs WHERE issue=2").fetchone()[0],
                    {"prepared", "running"},
                )
            recovery = Harness(
                github=HandoffGitHub([]), repositories=[], paths=runtime,
                workers=(DEFAULT_WORKERS[0], DEFAULT_WORKERS[4]),
                worker_runner=CompletingRunner(), workspace_manager=manager,
                now=lambda: NOW, enforce_contracts=False,
            ).tick()
            self.assertEqual([], recovery.assignments)
            self.assertEqual([], list(runtime.terminalization.glob("*.json")))
            recovered_payload = json.loads(by_issue[2].result_path.read_text())
            self.assertEqual("deleted", recovered_payload["cleanup"]["outcome"])
            self.assertEqual({"Example/pm", "Example/qa"}, {repo for repo, _ in verified})
            with sqlite3.connect(runtime.database) as connection:
                rows = connection.execute(
                    "SELECT state, outcome FROM runs ORDER BY issue"
                ).fetchall()
            self.assertEqual([("finished", "failed"), ("finished", "completed")], rows)

    def test_cleanup_exception_fails_originating_tick_and_recovers_next_tick(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            repo = root / "repo"
            repo.mkdir()
            candidate = Candidate(
                "Example/project", repo, 3, "plan", Role.PM,
                "to be planned", "P0", NOW,
            )
            source = ResolvedSource("default", "a" * 40, "main", None, None)

            class SourceGitHub(FakeGitHub):
                def resolve_source(inner_self, _candidate):
                    return source

                def verify_pm_handoff(inner_self, _candidate, _run_id):
                    return True

            class CleanupFailingManager:
                def __init__(inner_self):
                    inner_self.cleanup_calls = 0

                def intent(inner_self, run_id, repository, origin, resolved, role, issue, **_kwargs):
                    run_root = root / "workspaces" / run_id
                    return WorkspaceContext(
                        run_root / "worktree", resolved.sha, resolved.kind, None,
                        resolved.remote_branch, resolved.pr_number,
                        f"refs/loop-harness/{run_id}/source", run_root,
                        run_root / "git", origin,
                    )

                def prepare(inner_self, run_id, repository, origin, resolved, role, issue, **kwargs):
                    context = inner_self.intent(
                        run_id, repository, origin, resolved, role, issue, **kwargs
                    )
                    context.path.mkdir(parents=True)
                    context.git_dir.mkdir()
                    return context

                def inspect(inner_self, repository, context, role, status, verifier=None):
                    self.assertTrue(verifier(context.source_sha, False))
                    return WorkspaceOutcome(
                        context.source_sha, True, False, True, "pending", None
                    )

                def cleanup(inner_self, context, inspected):
                    inner_self.cleanup_calls += 1
                    if inner_self.cleanup_calls == 1:
                        return WorkspaceOutcome(
                            inspected.final_sha, inspected.clean,
                            inspected.push_verified, inspected.handoff_verified,
                            "pending", "workspace cleanup failed: injected",
                        )
                    import shutil
                    if context.run_root.exists():
                        shutil.rmtree(context.run_root)
                    return WorkspaceOutcome(
                        inspected.final_sha, inspected.clean,
                        inspected.push_verified, inspected.handoff_verified,
                        "deleted", None,
                    )

            class CompletingRunner:
                def run(inner_self, run_id, assignment, **kwargs):
                    path = ResultStore(runtime.results).write(
                        run_id, {"status": "completed", "exit_code": 0}
                    )
                    return WorkerResult(run_id, "completed", 0, path)

            manager = CleanupFailingManager()
            harness = Harness(
                github=SourceGitHub([candidate]), repositories=[], paths=runtime,
                workers=(DEFAULT_WORKERS[0],), worker_runner=CompletingRunner(),
                workspace_manager=manager, now=lambda: NOW, enforce_contracts=False,
            )
            with patch(
                "loop_harness.app.validate_repository_origin",
                return_value="https://github.com/Example/project.git",
            ):
                first = harness.tick()

            self.assertEqual("failed", first.results[0].status)
            self.assertNotEqual(0, first.results[0].exit_code)
            payload = json.loads(first.results[0].result_path.read_text())
            self.assertEqual("failed", payload["status"])
            self.assertEqual("pending", payload["cleanup"]["outcome"])
            self.assertEqual(1, len(list(runtime.terminalization.glob("*.json"))))
            with sqlite3.connect(runtime.database) as connection:
                self.assertIn(
                    connection.execute("SELECT state FROM runs").fetchone()[0],
                    {"prepared", "running"},
                )

            harness.github = SourceGitHub([])
            second = harness.tick()
            self.assertEqual([], second.failures)
            self.assertEqual([], list(runtime.terminalization.glob("*.json")))
            recovered = json.loads(first.results[0].result_path.read_text())
            self.assertEqual("completed", recovered["status"])
            self.assertEqual("deleted", recovered["cleanup"]["outcome"])

    def test_terminalization_journal_recovers_repeated_finish_failure_next_tick(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            candidate = Candidate(
                "Example/project", root, 9, "plan", Role.PM,
                "to be planned", "P0", NOW,
            )

            class CompletingRunner:
                def run(inner_self, run_id, assignment, **kwargs):
                    kwargs["on_started"](os.getpid(), "identity")
                    path = runtime.results / f"{run_id}.json"
                    path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
                    return WorkerResult(run_id, "completed", 0, path)

            original_finish = RunStore.finish
            failures_left = 3

            def flaky_finish(store, *args, **kwargs):
                nonlocal failures_left
                if failures_left:
                    failures_left -= 1
                    raise sqlite3.OperationalError("injected persistent finish failure")
                return original_finish(store, *args, **kwargs)

            harness = Harness(
                github=FakeGitHub([candidate]), repositories=[], paths=runtime,
                workers=(DEFAULT_WORKERS[0],), worker_runner=CompletingRunner(),
                now=lambda: NOW, enforce_contracts=False,
            )
            with patch("loop_harness.app.validate_repository_origin"), patch.object(
                RunStore, "finish", autospec=True, side_effect=flaky_finish
            ):
                first = harness.tick()
                self.assertEqual("failed", first.results[0].status)
                self.assertNotEqual(0, first.results[0].exit_code)
                failure_payload = json.loads(
                    first.results[0].result_path.read_text(encoding="utf-8")
                )
                self.assertEqual(1, failure_payload["exit_code"])
                self.assertEqual("persistence", failure_payload["failure_stage"])
                journals = list(runtime.terminalization.glob("*.json"))
                self.assertEqual(1, len(journals))
                with sqlite3.connect(runtime.database) as connection:
                    self.assertIn(
                        connection.execute("SELECT state FROM runs").fetchone()[0],
                        {"prepared", "running"},
                    )
                harness.github = FakeGitHub([])
                second = harness.tick()

            self.assertEqual([], second.assignments)
            self.assertEqual([], list(runtime.terminalization.glob("*.json")))
            with sqlite3.connect(runtime.database) as connection:
                self.assertEqual(
                    ("finished", "failed"),
                    connection.execute("SELECT state, outcome FROM runs").fetchone(),
                )

    def test_committed_finish_survives_readback_failure_without_changing_outcome(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            candidate = Candidate(
                "Example/project", root, 9, "plan", Role.PM,
                "to be planned", "P0", NOW,
            )

            class CompletingRunner:
                def run(inner_self, run_id, assignment, **kwargs):
                    path = ResultStore(runtime.results).write(
                        run_id, {"status": "completed", "issue": assignment.candidate.number}
                    )
                    return WorkerResult(run_id, "completed", 0, path)

            original_finish = RunStore.finish

            def committed_then_raised(store, *args, **kwargs):
                original_finish(store, *args, **kwargs)
                raise sqlite3.OperationalError("lost finish acknowledgement")

            with patch("loop_harness.app.validate_repository_origin"), patch.object(
                RunStore, "finish", autospec=True, side_effect=committed_then_raised
            ), patch.object(
                RunStore, "is_finished", autospec=True,
                side_effect=sqlite3.OperationalError("readback unavailable"),
            ):
                tick = Harness(
                    github=FakeGitHub([candidate]), repositories=[], paths=runtime,
                    workers=(DEFAULT_WORKERS[0],), worker_runner=CompletingRunner(),
                    now=lambda: NOW, enforce_contracts=False,
                ).tick()

            self.assertEqual("completed", tick.results[0].status)
            payload = json.loads(tick.results[0].result_path.read_text())
            self.assertEqual("completed", payload["status"])
            self.assertEqual([], list(runtime.terminalization.glob("*.json")))
            with sqlite3.connect(runtime.database) as connection:
                self.assertEqual(
                    ("finished", "completed"),
                    connection.execute("SELECT state, outcome FROM runs").fetchone(),
                )

    def test_committed_finish_with_unavailable_readback_recovers_desired_outcome(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            candidate = Candidate(
                "Example/project", root, 9, "plan", Role.PM,
                "to be planned", "P0", NOW,
            )

            class CompletingRunner:
                def run(inner_self, run_id, assignment, **kwargs):
                    path = ResultStore(runtime.results).write(
                        run_id, {"status": "completed", "issue": assignment.candidate.number}
                    )
                    return WorkerResult(run_id, "completed", 0, path)

            original_finish = RunStore.finish

            def committed_then_raised(store, *args, **kwargs):
                original_finish(store, *args, **kwargs)
                raise sqlite3.OperationalError("lost finish acknowledgement")

            with patch("loop_harness.app.validate_repository_origin"), patch.object(
                RunStore, "finish", autospec=True, side_effect=committed_then_raised
            ), patch.object(
                RunStore, "is_finished", autospec=True,
                side_effect=sqlite3.OperationalError("readback unavailable"),
            ), patch.object(
                RunStore, "get_run", autospec=True,
                side_effect=sqlite3.OperationalError("readback unavailable"),
            ):
                first = Harness(
                    github=FakeGitHub([candidate]), repositories=[], paths=runtime,
                    workers=(DEFAULT_WORKERS[0],), worker_runner=CompletingRunner(),
                    now=lambda: NOW, enforce_contracts=False,
                ).tick()

            self.assertEqual("failed", first.results[0].status)
            self.assertNotEqual(0, first.results[0].exit_code)
            payload = json.loads(first.results[0].result_path.read_text())
            self.assertEqual("completed", payload["status"])
            journals = list(runtime.terminalization.glob("*.json"))
            self.assertEqual(1, len(journals))
            self.assertEqual("completed", json.loads(journals[0].read_text())["outcome"])
            with sqlite3.connect(runtime.database) as connection:
                self.assertEqual(
                    ("finished", "completed"),
                    connection.execute("SELECT state, outcome FROM runs").fetchone(),
                )

            second = Harness(
                github=FakeGitHub([]), repositories=[], paths=runtime,
                workers=(DEFAULT_WORKERS[0],), worker_runner=ExplodingRunner(),
                now=lambda: NOW, enforce_contracts=False,
            ).tick()

            self.assertEqual([], second.failures)
            self.assertEqual([], list(runtime.terminalization.glob("*.json")))
            self.assertEqual("completed", json.loads(first.results[0].result_path.read_text())["status"])
            with sqlite3.connect(runtime.database) as connection:
                self.assertEqual(
                    ("finished", "completed", str(first.results[0].result_path)),
                    connection.execute(
                        "SELECT state, outcome, result_path FROM runs"
                    ).fetchone(),
                )

    def test_recovery_converges_temporary_result_mismatch_from_journal(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            runtime.ensure()
            candidate = Candidate(
                "Example/project", root, 9, "plan", Role.PM,
                "to be planned", "P0", NOW,
            )
            store = RunStore(runtime.database)
            self.assertTrue(store.reserve(Assignment(DEFAULT_WORKERS[0], candidate), "run-mismatch", NOW))
            result_path = ResultStore(runtime.results).write(
                "run-mismatch", {"status": "completed", "exit_code": 0}
            )
            ResultStore(runtime.terminalization).write("run-mismatch", {
                "schema_version": 1, "run_id": "run-mismatch",
                "repository": candidate.repo, "outcome": "failed",
                "result_path": str(result_path),
            })

            tick = Harness(
                github=FakeGitHub([]), repositories=[], paths=runtime,
                workers=(DEFAULT_WORKERS[0],), worker_runner=ExplodingRunner(),
                now=lambda: NOW, enforce_contracts=False,
            ).tick()

            self.assertEqual([], tick.failures)
            payload = json.loads(result_path.read_text())
            self.assertEqual("failed", payload["status"])
            self.assertNotEqual(0, payload["exit_code"])
            self.assertEqual([], list(runtime.terminalization.glob("*.json")))
            with sqlite3.connect(runtime.database) as connection:
                self.assertEqual(
                    ("finished", "failed"),
                    connection.execute("SELECT state, outcome FROM runs").fetchone(),
                )

    def test_live_run_uses_resolved_worktree_and_verifies_publication_before_cleanup(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            seed = root / "seed"
            canonical = root / "canonical"
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
            (seed / "README.md").write_text("old\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "old"], check=True)
            subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True)
            subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)
            subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
            subprocess.run(["git", "clone", "-q", str(remote), str(canonical)], check=True)
            (seed / "README.md").write_text("assigned\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "commit", "-qam", "assigned"], check=True)
            subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)
            source_sha = subprocess.run(
                ["git", "-C", str(seed), "rev-parse", "HEAD"], text=True,
                capture_output=True, check=True
            ).stdout.strip()
            candidate = Candidate(
                "Example/project", canonical, 7, "work", Role.DEV, "todo", "P0", NOW
            )

            class SourceGitHub(FakeGitHub):
                def resolve_source(inner_self, _candidate):
                    return ResolvedSource("default", source_sha, "main", None, None)

                def verify_dev_handoff(
                    inner_self, _candidate, branch, sha, run_id, *, has_changes
                ):
                    self.assertTrue(run_id.startswith("run-"))
                    return bool(branch and sha and has_changes)

            runtime = RuntimePaths(root / "runtime")

            class PublishingRunner:
                def run(inner_self, run_id, assignment, **kwargs):
                    workspace = assignment.workspace
                    self.assertIsNotNone(workspace)
                    self.assertNotEqual(canonical, workspace.path)
                    self.assertEqual("assigned\n", (workspace.path / "README.md").read_text())
                    kwargs["on_started"](os.getpid(), "identity")
                    subprocess.run(["git", "-C", str(workspace.path), "config", "user.email", "test@example.com"], check=True)
                    subprocess.run(["git", "-C", str(workspace.path), "config", "user.name", "Test"], check=True)
                    (workspace.path / "done.txt").write_text("done\n", encoding="utf-8")
                    subprocess.run(["git", "-C", str(workspace.path), "add", "done.txt"], check=True)
                    subprocess.run(["git", "-C", str(workspace.path), "commit", "-q", "-m", "done"], check=True)
                    subprocess.run([
                        "git", "-C", str(workspace.path), "push", "-q", "origin",
                        f"HEAD:refs/heads/{workspace.remote_branch}"
                    ], check=True)
                    result_path = runtime.results / f"{run_id}.json"
                    result_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
                    return WorkerResult(run_id, "completed", 0, result_path)

            def clone_repository(_slug: str, destination: Path) -> None:
                subprocess.run(
                    ["git", "clone", "--bare", "-q", str(remote), str(destination)],
                    check=True,
                )

            def remote_head(_slug: str, branch: str) -> str | None:
                return subprocess.run(
                    ["git", "--git-dir", str(remote), "rev-parse", "--verify", f"refs/heads/{branch}"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip()

            with patch(
                "loop_harness.app.validate_repository_origin", return_value=str(remote)
            ):
                result = Harness(
                    github=SourceGitHub([candidate]), repositories=[], paths=runtime,
                    workers=(DEFAULT_WORKERS[1],), worker_runner=PublishingRunner(),
                    workspace_manager=WorkspaceManager(
                        root / "workspaces",
                        clone_repository=clone_repository,
                        remote_head=remote_head,
                    ),
                    now=lambda: NOW, enforce_contracts=False,
                ).tick()

            self.assertEqual("completed", result.results[0].status)
            payload = json.loads(result.results[0].result_path.read_text())
            self.assertTrue(payload["push_verified"])
            self.assertTrue(payload["handoff_verified"])
            self.assertEqual("deleted", payload["cleanup"]["outcome"])
            self.assertFalse(Path(payload["workspace_path"]).exists())
            self.assertEqual("old\n", (canonical / "README.md").read_text())

    def test_shadow_blocks_repository_with_unsynchronized_contracts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = Candidate(
                repo="Example/project",
                repo_path=root,
                number=1,
                title="Unsafe contract",
                role=Role.DEV,
                state="todo",
                priority="P0",
                state_entered_at=NOW,
            )
            with self.assertRaisesRegex(ValueError, "contract.*not synchronized"):
                Harness(
                    github=FakeGitHub([candidate]),
                    repositories=[],
                    paths=RuntimePaths(root / "runtime"),
                    worker_runner=ExplodingRunner(),
                    now=lambda: NOW,
                    enforce_contracts=True,
                ).tick(dry_run=True)

    def test_origin_validation_rejects_github_substring_hosts(self) -> None:
        with TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "repo"
            subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
            subprocess.run(
                ["git", "-C", str(repo_path), "remote", "add", "origin", "https://evilgithub.com/Example/project.git"],
                check=True,
            )
            with self.assertRaisesRegex(ValueError, "origin mismatch"):
                validate_repository_origin(repo_path, "Example/project")
            subprocess.run(
                ["git", "-C", str(repo_path), "remote", "set-url", "origin", "ssh://git@notgithub.com/Example/project.git"],
                check=True,
            )
            with self.assertRaisesRegex(ValueError, "origin mismatch"):
                validate_repository_origin(repo_path, "Example/project")

    def test_origin_validation_accepts_only_canonical_github_forms(self) -> None:
        accepted = (
            "https://github.com/Example/project",
            "https://github.com/Example/project.git",
            "git@github.com:Example/project",
            "git@github.com:Example/project.git",
            "ssh://git@github.com/Example/project",
            "ssh://git@github.com/Example/project.git",
        )
        rejected = (
            "http://github.com/Example/project.git",
            "https://user@github.com/Example/project.git",
            "https://github.com:443/Example/project.git",
            "https://github.com/Example/project.git?token=secret",
            "https://github.com/Example/project.git#fragment",
            "ssh://root@github.com/Example/project.git",
            "ssh://git:secret@github.com/Example/project.git",
            "ssh://git@github.com:22/Example/project.git",
            "git+ssh://git@github.com/Example/project.git",
            "github.com:Example/project.git",
            "git@github.com:Example/project/extra.git",
        )
        with TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "repo"
            subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
            subprocess.run(
                ["git", "-C", str(repo_path), "remote", "add", "origin", accepted[0]],
                check=True,
            )
            for origin in accepted:
                subprocess.run(
                    ["git", "-C", str(repo_path), "remote", "set-url", "origin", origin],
                    check=True,
                )
                validate_repository_origin(repo_path, "Example/project")
            for origin in rejected:
                subprocess.run(
                    ["git", "-C", str(repo_path), "remote", "set-url", "origin", origin],
                    check=True,
                )
                with self.assertRaisesRegex(ValueError, "origin mismatch"):
                    validate_repository_origin(repo_path, "Example/project")

    def test_origin_validation_error_does_not_echo_credentials(self) -> None:
        credential = "super-secret-origin-password"
        with TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "repo"
            subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
            subprocess.run(
                [
                    "git", "-C", str(repo_path), "remote", "add", "origin",
                    f"https://user:{credential}@github.com/Example/project.git",
                ],
                check=True,
            )
            with self.assertRaises(ValueError) as raised:
                validate_repository_origin(repo_path, "Example/project")
            self.assertNotIn(credential, str(raised.exception))
            self.assertNotIn("user:", str(raised.exception))

    def test_origin_validation_rejects_noncanonical_push_destination(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/Example/project.git"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "remote", "set-url", "--push", "origin", "https://evil.example/exfil.git"],
                check=True,
            )
            with self.assertRaisesRegex(ValueError, "origin mismatch"):
                validate_repository_origin(repo, "Example/project")

    def test_dry_run_plans_six_lanes_without_creating_runtime_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "does-not-exist")
            candidates = []
            for index, worker in enumerate(DEFAULT_WORKERS, start=1):
                state = {Role.PM: "to be planned", Role.DEV: "todo", Role.QA: "qa ready"}[worker.role]
                candidates.append(
                    Candidate(
                        repo="Example/project",
                        repo_path=root,
                        number=index,
                        title=f"Issue {index}",
                        role=worker.role,
                        state=state,
                        priority="P1",
                        state_entered_at=NOW,
                    )
                )
            harness = Harness(
                github=FakeGitHub(candidates),
                repositories=[object()],
                paths=runtime,
                workers=DEFAULT_WORKERS,
                worker_runner=ExplodingRunner(),
                now=lambda: NOW,
                enforce_contracts=False,
            )
            result = harness.tick(dry_run=True)

            self.assertEqual("dry-run", result.mode)
            self.assertEqual(6, len(result.assignments))
            self.assertFalse(runtime.root.exists())

    def test_empty_dry_run_is_silent_plan(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = RuntimePaths(Path(tmp) / "runtime")
            result = Harness(
                github=FakeGitHub([]),
                repositories=[],
                paths=runtime,
                workers=DEFAULT_WORKERS,
                worker_runner=ExplodingRunner(),
                now=lambda: NOW,
                enforce_contracts=False,
            ).tick(dry_run=True)
            self.assertEqual([], result.assignments)
            self.assertFalse(runtime.root.exists())

    def test_dry_run_reads_existing_database_without_writing_and_excludes_active_work(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            runtime.ensure()
            active = Candidate(
                repo="Example/project",
                repo_path=root,
                number=1,
                title="Active",
                role=Role.PM,
                state="to be planned",
                priority="P0",
                state_entered_at=NOW,
            )
            queued = Candidate(
                repo="Example/project",
                repo_path=root,
                number=2,
                title="Queued",
                role=Role.PM,
                state="to be planned",
                priority="P1",
                state_entered_at=NOW,
            )
            store = RunStore(runtime.database)
            self.assertTrue(store.reserve(Assignment(DEFAULT_WORKERS[0], active), "active", NOW))
            before = {
                path.relative_to(runtime.root): path.read_bytes()
                for path in runtime.root.rglob("*")
                if path.is_file() and not path.name.endswith("-shm")
            }

            result = Harness(
                github=FakeGitHub([active, queued]),
                repositories=[],
                paths=runtime,
                workers=(DEFAULT_WORKERS[0],),
                worker_runner=ExplodingRunner(),
                now=lambda: NOW,
                enforce_contracts=False,
            ).tick(dry_run=True)

            after = {
                path.relative_to(runtime.root): path.read_bytes()
                for path in runtime.root.rglob("*")
                if path.is_file() and not path.name.endswith("-shm")
            }
            self.assertEqual([], result.assignments)
            self.assertEqual(before, after)

    def test_live_tick_rejects_repository_origin_mismatch_before_reservation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root / "repo")], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root / "repo"),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/Other/project.git",
                ],
                check=True,
            )
            candidate = Candidate(
                repo="Example/project",
                repo_path=root / "repo",
                number=1,
                title="Mismatched origin",
                role=Role.PM,
                state="to be planned",
                priority="P0",
                state_entered_at=NOW,
            )
            runtime = RuntimePaths(root / "runtime")
            harness = Harness(
                github=FakeGitHub([candidate]),
                repositories=[object()],
                paths=runtime,
                worker_runner=ExplodingRunner(),
                now=lambda: NOW,
                enforce_contracts=False,
            )

            result = harness.tick()
            self.assertEqual([], result.assignments)
            self.assertEqual([], result.results)
            self.assertEqual(1, len(result.failures))
            self.assertEqual("Example/project", result.failures[0].repository)
            self.assertEqual("origin", result.failures[0].stage)
            self.assertIn("origin mismatch", result.failures[0].error)
            if runtime.database.exists():
                with sqlite3.connect(runtime.database) as connection:
                    self.assertEqual(
                        0, connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                    )

    def test_reservation_conflict_backfills_same_profile_with_next_issue(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimePaths(root / "runtime")
            candidates = [
                Candidate(
                    repo="Example/project",
                    repo_path=root,
                    number=number,
                    title=f"Issue {number}",
                    role=Role.PM,
                    state="to be planned",
                    priority="P0" if number == 1 else "P1",
                    state_entered_at=NOW,
                )
                for number in (1, 2)
            ]

            class ConflictingStore:
                def __init__(inner_self, _path):
                    inner_self.attempts: list[int] = []

                def reclaim_stale(inner_self, *_args):
                    return []

                def active_profiles(inner_self):
                    return set()

                def active_issues(inner_self):
                    return set()

                def reserve(inner_self, item, _run_id, _at):
                    inner_self.attempts.append(item.candidate.number)
                    return item.candidate.number != 1

                def mark_running(inner_self, *_args, **_kwargs):
                    return None

                def finish(inner_self, *_args, **_kwargs):
                    return None

            class CompletingRunner:
                def run(inner_self, run_id, item, **kwargs):
                    kwargs["on_started"](os.getpid(), "identity")
                    result_path = runtime.results / f"{run_id}.json"
                    result_path.write_text("{}", encoding="utf-8")
                    return WorkerResult(run_id, "completed", 0, result_path)

            with (
                patch("loop_harness.app.RunStore", ConflictingStore),
                patch("loop_harness.app.validate_repository_origin"),
            ):
                result = Harness(
                    github=FakeGitHub(candidates),
                    repositories=[],
                    paths=runtime,
                    workers=(DEFAULT_WORKERS[0],),
                    worker_runner=CompletingRunner(),
                    now=lambda: NOW,
                    enforce_contracts=False,
                ).tick()

            self.assertEqual([2], [item.candidate.number for item in result.assignments])

    def test_claim_lock_is_released_before_worker_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
            subprocess.run(
                [
                    "git", "-C", str(repo_path), "remote", "add", "origin",
                    "https://github.com/Example/project.git",
                ],
                check=True,
            )
            runtime = RuntimePaths(root / "runtime")
            candidate = Candidate(
                repo="Example/project",
                repo_path=repo_path,
                number=1,
                title="One issue",
                role=Role.PM,
                state="to be planned",
                priority="P0",
                state_entered_at=NOW,
            )

            class CheckingRunner:
                def run(inner_self, run_id, assignment, **kwargs):
                    probe = FileLock(runtime.claim_lock)
                    self.assertTrue(probe.try_acquire())
                    probe.release()
                    kwargs["on_started"](os.getpid(), "test-process")
                    result_path = runtime.results / f"{run_id}.json"
                    result_path.write_text("{}", encoding="utf-8")
                    return WorkerResult(run_id, "completed", 0, result_path)

            result = Harness(
                github=FakeGitHub([candidate]),
                repositories=[],
                paths=runtime,
                worker_runner=CheckingRunner(),
                now=lambda: NOW,
                enforce_contracts=False,
            ).tick()
            self.assertEqual(1, len(result.results))


if __name__ == "__main__":
    unittest.main()
