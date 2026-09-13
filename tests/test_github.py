from __future__ import annotations

import json
import subprocess
import unittest
from datetime import UTC, datetime
from pathlib import Path

from loop_harness.config import RepositoryConfig
from loop_harness.github import GitHubClient, GitHubError
from loop_harness.models import Role


class GitHubClientTests(unittest.TestCase):
    def test_scan_uses_one_snapshot_and_maps_labels_to_roles(self) -> None:
        calls: list[list[str]] = []

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            rows = [
                {
                    "number": 12,
                    "title": "Implement safely",
                    "labels": [{"name": "todo"}, {"name": "P1"}],
                    "createdAt": "2026-09-10T00:00:00Z",
                    "updatedAt": "2026-09-11T00:00:00Z",
                }
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")

        repo = RepositoryConfig(
            "Example/project", Path("/srv/project"), True, (Role.PM, Role.DEV, Role.QA)
        )
        candidates = GitHubClient(runner=runner).scan([repo])

        self.assertEqual([("Example/project", 12)], [item.identity for item in candidates])
        self.assertEqual(Role.DEV, candidates[0].role)
        self.assertEqual("P1", candidates[0].priority)
        self.assertEqual(1, len(calls))
        self.assertTrue(all(call[:3] == ["gh", "issue", "list"] for call in calls))
        self.assertTrue(all("--state" in call and "open" in call for call in calls))
        self.assertTrue(all("--label" not in call and "1000" in call for call in calls))
        forbidden = {"edit", "comment", "close", "reopen"}
        self.assertFalse(any(forbidden.intersection(call) for call in calls))

    def test_github_failure_is_visible_instead_of_becoming_empty_queue(self) -> None:
        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, "", "authentication failed")

        repo = RepositoryConfig("Example/project", Path("/srv/project"), True, (Role.DEV,))
        with self.assertRaisesRegex(GitHubError, "authentication failed"):
            GitHubClient(runner=runner).scan([repo])

    def test_github_failure_redacts_credentials(self) -> None:
        secret = "github_pat_example-secret-value"

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv, 1, "", f"Authorization: Bearer {secret}"
            )

        repo = RepositoryConfig("Example/project", Path("/srv/project"), True, (Role.DEV,))
        with self.assertRaises(GitHubError) as raised:
            GitHubClient(runner=runner).scan([repo])
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_rejects_malformed_github_output(self) -> None:
        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "not-json", "")

        repo = RepositoryConfig("Example/project", Path("/srv/project"), True, (Role.DEV,))
        with self.assertRaises(GitHubError):
            GitHubClient(runner=runner).scan([repo])

    def test_scan_quarantines_issue_with_multiple_workflow_states(self) -> None:
        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            rows = [
                {
                    "number": 13,
                    "title": "Ambiguous lifecycle",
                    "labels": [{"name": "todo"}, {"name": "qa ready"}, {"name": "P0"}],
                    "createdAt": "2026-09-10T00:00:00Z",
                    "updatedAt": "2026-09-11T00:00:00Z",
                }
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")

        repo = RepositoryConfig(
            "Example/project", Path("/srv/project"), True, (Role.PM, Role.DEV, Role.QA)
        )
        self.assertEqual([], GitHubClient(runner=runner).scan([repo]))

    def test_scan_quarantines_multiple_priorities_but_allows_missing_priority(self) -> None:
        rows = [
            {
                "number": 14,
                "title": "Ambiguous priority",
                "labels": [{"name": "todo"}, {"name": "P0"}, {"name": "high"}],
                "updatedAt": "2026-09-11T00:00:00Z",
            },
            {
                "number": 15,
                "title": "No priority",
                "labels": [{"name": "qa ready"}],
                "updatedAt": "2026-09-11T00:00:00Z",
            },
        ]

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")

        repo = RepositoryConfig(
            "Example/project", Path("/srv/project"), True, (Role.DEV, Role.QA)
        )
        candidates = GitHubClient(runner=runner).scan([repo])
        self.assertEqual([15], [item.number for item in candidates])
        self.assertEqual("missing", candidates[0].priority)


if __name__ == "__main__":
    unittest.main()
