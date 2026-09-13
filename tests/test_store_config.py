from __future__ import annotations

import json
import os
import sqlite3
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory


from loop_harness.config import ConfigError, load_registry
from loop_harness.models import Candidate, Role
from loop_harness.scheduler import Assignment, DEFAULT_WORKERS
from loop_harness.store import RunStore


NOW = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)


def assignment(profile: str = "mcgee", issue: int = 42) -> Assignment:
    worker = next(item for item in DEFAULT_WORKERS if item.profile == profile)
    role = worker.role
    state = {Role.PM: "to be planned", Role.DEV: "todo", Role.QA: "qa ready"}[role]
    return Assignment(
        worker,
        Candidate(
            repo="example/project",
            repo_path=Path("/srv/project"),
            number=issue,
            title="Test",
            role=role,
            state=state,
            priority="high",
            state_entered_at=NOW,
        ),
    )


class RegistryTests(unittest.TestCase):
    def test_loads_enabled_repositories_and_validates_unique_slug_and_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "one"
            second = root / "two"
            first.mkdir()
            second.mkdir()
            config_path = root / "repositories.json"
            config_path.write_text(
                json.dumps(
                    {
                        "repositories": [
                            {
                                "slug": "Example/one",
                                "path": str(first),
                                "enabled": True,
                                "roles": ["pm", "dev", "qa"],
                            },
                            {
                                "slug": "Example/two",
                                "path": str(second),
                                "enabled": False,
                                "roles": ["dev"],
                            },
                        ]
                    }
                )
            )
            registry = load_registry(config_path)
            self.assertEqual(["Example/one"], [repo.slug for repo in registry.enabled])
            self.assertEqual((Role.PM, Role.DEV, Role.QA), registry.enabled[0].roles)

            config_path.write_text(
                json.dumps(
                    {
                        "repositories": [
                            {"slug": "Example/one", "path": str(first), "enabled": True},
                            {"slug": "Example/one", "path": str(second), "enabled": True},
                        ]
                    }
                )
            )
            with self.assertRaises(ConfigError):
                load_registry(config_path)

    def test_registry_rejects_non_boolean_enabled_value(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            repo_path.mkdir()
            config_path = root / "repositories.json"
            config_path.write_text(
                json.dumps(
                    {
                        "repositories": [
                            {
                                "slug": "Example/project",
                                "path": str(repo_path),
                                "enabled": "false",
                                "roles": ["dev"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "enabled must be a boolean"):
                load_registry(config_path)


class StoreTests(unittest.TestCase):
    def test_workspace_recovery_context_is_persisted_with_active_run(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            self.assertTrue(store.reserve(assignment(), "run-context", NOW))
            context = {"path": "/private/run/worktree", "source_sha": "a" * 40}
            store.attach_workspace("run-context", context)
            row = store.get_run("run-context")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(context, json.loads(row["workspace_json"]))

    def test_read_only_wal_access_creates_no_snapshot_or_temporary_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "runs.sqlite3"
            store = RunStore(path)
            writer = sqlite3.connect(path)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            self.assertTrue(store.reserve(assignment(), "active", NOW))
            before = {item.name: item.stat().st_mtime_ns for item in root.iterdir()}

            from unittest.mock import patch
            with patch("tempfile.TemporaryDirectory", side_effect=AssertionError("snapshot write")), \
                 patch("tempfile.mkstemp", side_effect=AssertionError("temporary write")):
                reader = RunStore(path, read_only=True)
                try:
                    self.assertEqual({"mcgee"}, reader.active_profiles())
                finally:
                    reader.close()

            after = {item.name: item.stat().st_mtime_ns for item in root.iterdir()}
            writer.close()
            self.assertEqual(before, after)

    def test_read_only_snapshot_reads_committed_database_state(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            store = RunStore(path)
            self.assertTrue(store.reserve(assignment(), "active", NOW))
            snapshot = RunStore(path, read_only=True)
            try:
                self.assertEqual({"mcgee"}, snapshot.active_profiles())
                self.assertEqual({("example/project", 42)}, snapshot.active_issues())
            finally:
                snapshot.close()

    def test_concurrent_reservation_allows_one_active_issue_and_profile(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.sqlite3"
            barrier = threading.Barrier(12)
            results: list[bool] = []
            guard = threading.Lock()

            def attempt(index: int) -> None:
                store = RunStore(db)
                barrier.wait()
                reserved = store.reserve(assignment(issue=42), f"run-{index}", NOW)
                with guard:
                    results.append(reserved)

            threads = [threading.Thread(target=attempt, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(1, sum(results))

            store = RunStore(db)
            self.assertFalse(store.reserve(assignment(issue=43), "same-profile", NOW))
            self.assertFalse(store.reserve(assignment(profile="torres", issue=42), "same-issue", NOW))
            self.assertEqual({("example/project", 42)}, store.active_issues())

    def test_stale_claim_is_not_reclaimed_before_45_minutes_even_if_dead(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            self.assertTrue(store.reserve(assignment(), "run-1", NOW))
            store.mark_running("run-1", pid=999999, process_start="missing", at=NOW)

            self.assertEqual([], store.reclaim_stale(NOW + timedelta(minutes=44, seconds=59), lambda *_: False))
            self.assertEqual(["run-1"], store.reclaim_stale(NOW + timedelta(minutes=45), lambda *_: False))
            self.assertTrue(store.reserve(assignment(), "run-2", NOW + timedelta(minutes=45)))

    def test_live_process_prevents_reclaim_after_stale_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.reserve(assignment(), "run-1", NOW)
            store.mark_running("run-1", pid=os.getpid(), process_start="token", at=NOW)
            reclaimed = store.reclaim_stale(
                NOW + timedelta(hours=2),
                lambda pid, start: pid == os.getpid() and start == "token",
            )
            self.assertEqual([], reclaimed)


if __name__ == "__main__":
    unittest.main()
