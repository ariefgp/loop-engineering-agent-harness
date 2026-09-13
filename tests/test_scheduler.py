from __future__ import annotations

import random
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loop_harness.models import Candidate, Role, deduplicate_and_sort
from loop_harness.scheduler import DEFAULT_WORKERS, build_profile_argv, plan_assignments


NOW = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)


def candidate(
    number: int,
    role: Role,
    state: str,
    *,
    repo: str = "example/project",
    priority: str = "medium",
    age_minutes: int = 10,
) -> Candidate:
    return Candidate(
        repo=repo,
        repo_path=Path("/srv/repos/project"),
        number=number,
        title=f"Issue {number}",
        role=role,
        state=state,
        priority=priority,
        state_entered_at=NOW - timedelta(minutes=age_minutes),
    )


class SchedulerTests(unittest.TestCase):
    def test_builds_safe_hermes_profile_argv_for_every_lane(self) -> None:
        expected = {"gibbs", "mcgee", "torres", "kate", "jimmy", "ducky"}
        self.assertEqual(expected, {worker.profile for worker in DEFAULT_WORKERS})

        for worker in DEFAULT_WORKERS:
            argv = build_profile_argv(
                worker,
                Path("/srv/repos/project with spaces"),
                run_budget_seconds=2700,
            )
            self.assertEqual(worker.executable, argv[0])
            self.assertEqual(
                [
                    worker.executable,
                    "chat",
                    "--query-file",
                    "-",
                    "--oneshot",
                    "-Q",
                    "--in",
                    "/srv/repos/project with spaces",
                    "--run-budget",
                    "2700",
                ],
                argv,
            )
            self.assertNotIn("claude", " ".join(argv).lower())

    def test_plans_all_six_lanes_without_exceeding_role_capacity(self) -> None:
        candidates = [
            candidate(1, Role.PM, "to be planned"),
            *[candidate(number, Role.DEV, "todo") for number in range(2, 7)],
            *[candidate(number, Role.QA, "qa ready") for number in range(7, 11)],
        ]
        assignments = plan_assignments(candidates, DEFAULT_WORKERS, busy_profiles=set())

        self.assertEqual(6, len(assignments))
        self.assertEqual(1, sum(a.candidate.role is Role.PM for a in assignments))
        self.assertEqual(3, sum(a.candidate.role is Role.DEV for a in assignments))
        self.assertEqual(2, sum(a.candidate.role is Role.QA for a in assignments))
        self.assertEqual(6, len({a.worker.profile for a in assignments}))
        self.assertEqual(6, len({a.candidate.identity for a in assignments}))

    def test_planning_excludes_active_issues_as_well_as_busy_profiles(self) -> None:
        candidates = [
            candidate(1, Role.DEV, "todo", priority="urgent"),
            candidate(2, Role.DEV, "todo", priority="high"),
            candidate(3, Role.DEV, "todo", priority="medium"),
        ]

        assignments = plan_assignments(
            candidates,
            DEFAULT_WORKERS,
            busy_profiles={"mcgee"},
            active_issues={("example/project", 1)},
        )

        self.assertEqual([2, 3], [item.candidate.number for item in assignments])
        self.assertEqual(["torres", "kate"], [item.worker.profile for item in assignments])

    def test_resume_priority_age_and_identity_make_order_deterministic(self) -> None:
        candidates = [
            candidate(9, Role.DEV, "todo", priority="urgent", age_minutes=1),
            candidate(8, Role.DEV, "in progress", priority="low", age_minutes=1),
            candidate(7, Role.DEV, "todo", priority="high", age_minutes=100),
            candidate(6, Role.DEV, "todo", priority="high", age_minutes=200),
            candidate(6, Role.DEV, "todo", priority="high", age_minutes=200),
            candidate(6, Role.DEV, "todo", repo="another/project", priority="high", age_minutes=200),
        ]
        expected = [
            ("example/project", 8),
            ("example/project", 9),
            ("another/project", 6),
            ("example/project", 6),
            ("example/project", 7),
        ]
        for seed in range(10):
            shuffled = list(candidates)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(expected, [c.identity for c in deduplicate_and_sort(shuffled)])


if __name__ == "__main__":
    unittest.main()
