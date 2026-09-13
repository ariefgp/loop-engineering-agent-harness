from __future__ import annotations

import os
import unittest
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from loop_harness.app import Harness, validate_repository_origin
from loop_harness.models import Candidate, Role
from loop_harness.runtime import FileLock, RuntimePaths
from loop_harness.scheduler import Assignment, DEFAULT_WORKERS
from loop_harness.store import RunStore
from loop_harness.worker import WorkerResult


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

            with self.assertRaisesRegex(ValueError, "origin mismatch"):
                harness.tick()
            self.assertFalse(runtime.root.exists())

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
