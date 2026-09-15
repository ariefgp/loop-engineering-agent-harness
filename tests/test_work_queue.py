from __future__ import annotations

import sqlite3
import threading
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from loop_harness.models import (
    Candidate,
    ClaimedWorkUnit,
    ConflictRequest,
    LeaseMode,
    NodeType,
    Role,
    WorkUnit,
    WorkflowKind,
)
from loop_harness.scheduler import Assignment, DEFAULT_WORKERS
from loop_harness.store import RunStore


NOW = datetime(2026, 9, 14, tzinfo=UTC)


def work_unit(
    index: int,
    *,
    workflow_id: str = "workflow-1",
    profile: str | None = None,
    capabilities: tuple[str, ...] = ("read_source",),
    conflicts: tuple[ConflictRequest, ...] = (),
    priority: int = 10,
) -> WorkUnit:
    return WorkUnit(
        unit_id=f"unit-{index}",
        workflow_id=workflow_id,
        node_id=f"node-{index}",
        workflow_kind=WorkflowKind.GRAPH,
        node_type=NodeType.AGENT,
        profile=profile,
        capabilities=capabilities,
        conflicts=conflicts,
        payload={"task": f"audit-{index}"},
        priority=priority,
        created_at=NOW,
    )


def v2_assignment(profile: str = "mcgee", issue: int = 5) -> Assignment:
    worker = next(item for item in DEFAULT_WORKERS if item.profile == profile)
    return Assignment(
        worker,
        Candidate(
            repo="example/project",
            repo_path=Path("/srv/project"),
            number=issue,
            title="Test",
            role=worker.role,
            state="todo",
            priority="high",
            state_entered_at=NOW,
        ),
    )


def authorize_test_transitions(connection: sqlite3.Connection) -> None:
    """Permit deliberate corruption setup on a test-only connection."""
    connection.create_function("runstore_transition_authorized", 0, lambda: 1)


class QueueTests(unittest.TestCase):
    def test_artifact_registration_validates_fenced_provenance_and_safe_location(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1)])
            claim = store.claim_next("mcgee", {"read_source"}, 123, "pid-start", NOW)
            assert claim is not None
            digest = "a" * 64
            arguments = (
                "artifact-1", "workflow-1", "node-1", claim.attempt_id,
                claim.claim_token, "audit_report", 1, "f" * 40, digest,
                {"summary": "one finding"}, f"artifacts/sha256/{digest}", NOW,
            )
            self.assertTrue(store.register_artifact(*arguments))
            self.assertFalse(store.register_artifact(*arguments))

            invalid = (
                ("b" * 63, f"artifacts/sha256/{'b' * 63}", {"summary": "ok"}, "SHA-256"),
                (digest, f"/tmp/{digest}", {"summary": "ok"}, "location"),
                (digest, f"artifacts/../{digest}", {"summary": "ok"}, "location"),
                (digest, "artifacts/report.json", {"summary": "ok"}, "content-addressed"),
                (digest, f"artifacts/sha256/{digest}", {"text": "x" * 65_536}, "exceeds"),
            )
            for index, (sha256, location, summary, message) in enumerate(invalid, 2):
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    store.register_artifact(
                        f"artifact-{index}", "workflow-1", "node-1", claim.attempt_id,
                        claim.claim_token, "audit_report", 1, "f" * 40, sha256,
                        summary, location, NOW,
                    )

            store.enqueue_workflow("workflow-2", WorkflowKind.GRAPH, "example/other", 6, {}, NOW)
            with self.assertRaisesRegex(ValueError, "provenance"):
                store.register_artifact(
                    "artifact-cross", "workflow-2", "node-1", claim.attempt_id,
                    claim.claim_token, "audit_report", 1, "f" * 40, digest,
                    {"summary": "wrong workflow"}, f"artifacts/sha256/{digest}", NOW,
                )
            with self.assertRaisesRegex(KeyError, "current claim"):
                store.register_artifact(
                    "artifact-fenced", "workflow-1", "node-1", claim.attempt_id,
                    "stale-token", "audit_report", 1, "f" * 40, digest,
                    {"summary": "stale"}, f"artifacts/sha256/{digest}", NOW,
                )

    def test_raw_state_updates_are_denied_with_and_without_conflict_leases(self) -> None:
        for conflicts in ((), (ConflictRequest("repo:example/project", LeaseMode.EXCLUSIVE),)):
            with self.subTest(conflicts=conflicts), TemporaryDirectory() as tmp:
                store = RunStore(Path(tmp) / "runs.sqlite3")
                store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
                store.enqueue_units("workflow-1", [work_unit(1, conflicts=conflicts)])
                if conflicts:
                    self.assertIsNotNone(store.claim_next(
                        "mcgee", {"read_source"}, 123, "start", NOW
                    ))
                with sqlite3.connect(store.path) as connection:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "runstore_transition_authorized"):
                        connection.execute(
                            "UPDATE graph_nodes SET state='failed' WHERE node_id='node-1'"
                        )

    def test_transition_authorization_resets_after_internal_exception(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow(
                "workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW
            )
            store.enqueue_units("workflow-1", [work_unit(1)])

            with store._connect() as connection:
                self.assertFalse(getattr(connection, "_runstore_transition_authorized"))
                with self.assertRaisesRegex(RuntimeError, "transition exploded"):
                    with store._authorized_transitions(connection):
                        self.assertTrue(getattr(connection, "_runstore_transition_authorized"))
                        raise RuntimeError("transition exploded")
                self.assertFalse(getattr(connection, "_runstore_transition_authorized"))
                with self.assertRaisesRegex(sqlite3.IntegrityError, "unauthorized"):
                    connection.execute(
                        "UPDATE graph_nodes SET state='blocked' WHERE node_id='node-1'"
                    )

    def test_claim_materializes_before_commit_and_never_orphans_malformed_work(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1)])
            with sqlite3.connect(store.path) as connection:
                connection.execute("UPDATE work_units SET payload_json='[]' WHERE unit_id='unit-1'")

            with self.assertRaisesRegex(ValueError, "payload"):
                store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            with sqlite3.connect(store.path) as connection:
                state = connection.execute(
                    "SELECT state FROM work_units WHERE unit_id='unit-1'"
                ).fetchone()[0]
                attempts = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            self.assertEqual("ready", state)
            self.assertEqual(0, attempts)

    def test_graph_mutations_enforce_dag_depth_state_and_failure_propagation(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            units = [work_unit(index) for index in range(1, 8)]
            store.enqueue_units("workflow-1", units[:6], [
                (f"node-{index}", f"node-{index + 1}", "required_success")
                for index in range(1, 6)
            ])
            with self.assertRaisesRegex(ValueError, "depth"):
                store.enqueue_units("workflow-1", [units[6]], [("node-6", "node-7", "required_success")])
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(0, connection.execute(
                    "SELECT COUNT(*) FROM graph_nodes WHERE node_id='node-7'"
                ).fetchone()[0])
            with self.assertRaisesRegex(ValueError, "cycle"):
                store.enqueue_units("workflow-1", [], [("node-6", "node-1", "required_success")])

        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-2", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            items = [work_unit(index, workflow_id="workflow-2", priority=20-index) for index in range(10, 13)]
            store.enqueue_units("workflow-2", items)
            first = store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            assert first is not None
            with self.assertRaisesRegex(ValueError, "successor.*claimed"):
                store.enqueue_units("workflow-2", [], [("node-11", first.work_unit.node_id, "required_success")])
            store.complete_attempt(first.attempt_id, first.claim_token, "succeeded", {"schema_version": 1, "summary": "ok"}, NOW, "done:first")
            self.assertEqual(0, store.enqueue_units(
                "workflow-2", [], [(first.work_unit.node_id, "node-11", "required_success")]
            ))
            second = store.claim_next("torres", {"read_source"}, 124, "start-2", NOW)
            self.assertIsNotNone(second, "late satisfied edge must not block its successor")

        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-3", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            items = [work_unit(index, workflow_id="workflow-3", priority=20-index) for index in range(20, 23)]
            store.enqueue_units("workflow-3", items, [
                ("node-20", "node-21", "required_success"),
                ("node-21", "node-22", "required_success"),
            ])
            first = store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            assert first is not None
            store.complete_attempt(first.attempt_id, first.claim_token, "failed", {"schema_version": 1, "summary": "failed"}, NOW, "failed:first")
            with sqlite3.connect(store.path) as connection:
                states = connection.execute(
                    "SELECT node_id, state FROM graph_nodes WHERE workflow_id='workflow-3' ORDER BY node_id"
                ).fetchall()
                workflow_state = connection.execute(
                    "SELECT state FROM workflow_instances WHERE workflow_id='workflow-3'"
                ).fetchone()[0]
            self.assertEqual([("node-20", "failed"), ("node-21", "cancelled"), ("node-22", "cancelled")], states)
            self.assertEqual("failed", workflow_state)

    def test_enqueue_validates_and_canonicalizes_queue_contract(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            with self.assertRaises(ValueError):
                store.enqueue_workflow("", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            with self.assertRaisesRegex(ValueError, "JSON object"):
                store.enqueue_workflow("bad-payload", WorkflowKind.GRAPH, "example/project", 5, [], NOW)  # type: ignore[arg-type]

            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            invalid_units = [
                WorkUnit("", "workflow-1", "node-x", WorkflowKind.GRAPH, NodeType.AGENT, None, ("read",), (), {}, 1, NOW),
                WorkUnit("unit-x", "workflow-1", "", WorkflowKind.GRAPH, NodeType.AGENT, None, ("read",), (), {}, 1, NOW),
                WorkUnit("unit-x", "workflow-1", "node-x", WorkflowKind.SINGLE, NodeType.AGENT, None, ("read",), (), {}, 1, NOW),
                WorkUnit("unit-x", "workflow-1", "node-x", WorkflowKind.GRAPH, NodeType.AGENT, "", ("read",), (), {}, 1, NOW),
                WorkUnit("unit-x", "workflow-1", "node-x", WorkflowKind.GRAPH, NodeType.AGENT, None, (), (), {}, 1, NOW),
                WorkUnit("unit-x", "workflow-1", "node-x", WorkflowKind.GRAPH, NodeType.AGENT, None, ("",), (), {}, 1, NOW),
                WorkUnit("unit-x", "workflow-1", "node-x", WorkflowKind.GRAPH, NodeType.AGENT, None, ("read",), (), [], 1, NOW),  # type: ignore[arg-type]
                WorkUnit("unit-x", "workflow-1", "node-x", WorkflowKind.GRAPH, NodeType.AGENT, None, ("read",), (ConflictRequest("", LeaseMode.SHARED),), {}, 1, NOW),
                WorkUnit("unit-x", "workflow-1", "node-x", WorkflowKind.GRAPH, NodeType.AGENT, None, ("read",), (ConflictRequest("repo:x", LeaseMode.SHARED), ConflictRequest("repo:x", LeaseMode.EXCLUSIVE)), {}, 1, NOW),
            ]
            for unit in invalid_units:
                with self.subTest(unit=unit):
                    with self.assertRaises(ValueError):
                        store.enqueue_units("workflow-1", [unit])

            offset_time = datetime(2026, 9, 14, 7, tzinfo=timezone(timedelta(hours=7)))
            store.enqueue_units("workflow-1", [WorkUnit(
                "unit-tz", "workflow-1", "node-tz", WorkflowKind.GRAPH, NodeType.AGENT,
                None, ("read_source",), (), {}, 1, offset_time,
            )])
            claim = store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            self.assertIsNotNone(claim)
            with sqlite3.connect(store.path) as connection:
                stored = connection.execute(
                    "SELECT created_at, available_at FROM work_units WHERE unit_id='unit-tz'"
                ).fetchone()
            self.assertEqual((NOW.isoformat(), NOW.isoformat()), stored)

            with sqlite3.connect(store.path) as connection:
                connection.execute("UPDATE workflow_instances SET state='succeeded' WHERE workflow_id='workflow-1'")
            with self.assertRaisesRegex(ValueError, "terminal workflow"):
                store.enqueue_units("workflow-1", [work_unit(9)])

    def test_active_ownership_unions_coherent_claims_with_legacy_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            for index, profile in ((1, "mcgee"), (2, "bishop")):
                workflow_id = f"workflow-{index}"
                store.enqueue_workflow(
                    workflow_id,
                    WorkflowKind.GRAPH,
                    f"example/project-{index}",
                    index,
                    {},
                    NOW,
                )
                store.enqueue_units(
                    workflow_id, [work_unit(index, workflow_id=workflow_id)]
                )
                self.assertIsNotNone(
                    store.claim_next(
                        profile, {"read_source"}, 120 + index, f"start-{index}", NOW
                    )
                )
            with sqlite3.connect(store.path) as connection:
                connection.executemany(
                    """INSERT INTO runs(
                        run_id, repo, issue, profile, role, state, created_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?, 'dev', 'running', ?, ?)""",
                    (
                        (
                            "legacy-duplicate",
                            "example/project-1",
                            1,
                            "mcgee",
                            NOW.isoformat(),
                            NOW.isoformat(),
                        ),
                        (
                            "legacy-distinct",
                            "example/legacy",
                            9,
                            "torres",
                            NOW.isoformat(),
                            NOW.isoformat(),
                        ),
                    ),
                )

            self.assertEqual({"mcgee", "bishop", "torres"}, store.active_profiles())
            self.assertEqual(
                {
                    ("example/project-1", 1),
                    ("example/project-2", 2),
                    ("example/legacy", 9),
                },
                store.active_issues(),
            )

    def test_v2_and_v3_ownership_is_reciprocal_for_profile_and_issue(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1)])
            claim = store.claim_next("mcgee", {"read_source"}, 123, "pid-start", NOW)
            self.assertIsNotNone(claim)

            self.assertFalse(store.reserve(v2_assignment(profile="mcgee", issue=6), "same-profile", NOW))
            self.assertFalse(store.reserve(v2_assignment(profile="torres", issue=5), "same-issue", NOW))
    def test_agent_result_is_a_strict_typed_versioned_report(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1)])
            claim = store.claim_next("mcgee", {"read_source"}, 123, "pid-start", NOW)
            assert claim is not None

            invalid_results = (
                ({"schema_version": 1, "summary": "ok", "status": "done"}, "unknown"),
                ({"schema_version": True, "summary": "ok"}, "schema_version"),
                ({"schema_version": 2, "summary": "ok"}, "schema_version"),
                ({"schema_version": 1, "summary": ["not text"]}, "summary"),
                ({"schema_version": 1, "summary": "ok", "findings": [{}]}, "finding"),
                ({"schema_version": 1, "summary": "ok", "findings": [
                    {"summary": "bad", "command_line": ["git", "push"]}
                ]}, "unknown"),
                ({"schema_version": 1, "summary": "ok", "artifact_ids": [1]}, "artifact"),
            )
            for result, message in invalid_results:
                with self.subTest(result=result), self.assertRaisesRegex(ValueError, message):
                    store.complete_attempt(
                        claim.attempt_id, claim.claim_token, "succeeded", result,
                        NOW + timedelta(minutes=1), "unsafe-completion",
                    )
            store.heartbeat_attempt(claim.attempt_id, claim.claim_token, NOW + timedelta(minutes=2))

            self.assertTrue(store.complete_attempt(
                claim.attempt_id, claim.claim_token, "succeeded",
                {
                    "schema_version": 1,
                    "summary": "one issue found",
                    "findings": [{
                        "summary": "unsafe interpolation", "path": "src/tool.py",
                        "line": 12, "severity": "high", "details": "Use parameters.",
                        "artifact_id": "artifact-1",
                    }],
                    "artifact_ids": ["artifact-1"],
                },
                NOW + timedelta(minutes=3), "safe-completion",
            ))

    def test_complete_attempt_is_fenced_idempotent_and_unlocks_required_successor(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units(
                "workflow-1",
                [work_unit(1, priority=20, conflicts=(
                    ConflictRequest("repo:example/project", LeaseMode.EXCLUSIVE),
                )), work_unit(2)],
                [("node-1", "node-2", "required_success")],
            )
            claim = store.claim_next("mcgee", {"read_source"}, 123, "pid-start", NOW)
            assert claim is not None

            self.assertTrue(store.complete_attempt(
                claim.attempt_id, claim.claim_token, "succeeded", {"schema_version": 1, "summary": "ok"},
                NOW + timedelta(minutes=1), "complete:attempt-1",
            ))
            self.assertFalse(store.complete_attempt(
                claim.attempt_id, claim.claim_token, "succeeded", {"schema_version": 1, "summary": "ok"},
                NOW + timedelta(minutes=1), "complete:attempt-1",
            ))
            with self.assertRaisesRegex(KeyError, "current claim"):
                store.complete_attempt(
                    claim.attempt_id, "stale", "succeeded", {"schema_version": 1, "summary": "ok"},
                    NOW + timedelta(minutes=1), "other-event",
                )
            successor = store.claim_next(
                "torres", {"read_source"}, 124, "pid-start-2", NOW + timedelta(minutes=2)
            )
            self.assertIsNotNone(successor)
            assert successor is not None
            self.assertEqual("unit-2", successor.work_unit.unit_id)
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(0, connection.execute(
                    "SELECT COUNT(*) FROM conflict_leases WHERE attempt_id=?", (claim.attempt_id,)
                ).fetchone()[0])
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(*) FROM events WHERE idempotency_key='complete:attempt-1'"
                ).fetchone()[0])

    def test_completion_rolls_back_when_unit_or_node_state_diverges(self) -> None:
        for table in ("work_units", "graph_nodes"):
            with self.subTest(table=table), TemporaryDirectory() as tmp:
                store = RunStore(Path(tmp) / "runs.sqlite3")
                store.enqueue_workflow(
                    "workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW
                )
                store.enqueue_units("workflow-1", [work_unit(1, conflicts=(
                    ConflictRequest("repo:example/project", LeaseMode.EXCLUSIVE),
                ))])
                claim = store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
                assert claim is not None
                with sqlite3.connect(store.path) as connection:
                    authorize_test_transitions(connection)
                    if table == "work_units":
                        connection.execute("DROP TRIGGER work_units_no_terminal_leases")
                    identifier = "unit-1" if table == "work_units" else "node-1"
                    key = "unit_id" if table == "work_units" else "node_id"
                    connection.execute(
                        f"UPDATE {table} SET state='ready' WHERE {key}=?", (identifier,)
                    )

                with self.assertRaisesRegex(RuntimeError, "state divergence"):
                    store.complete_attempt(
                        claim.attempt_id, claim.claim_token, "succeeded",
                        {"schema_version": 1, "summary": "ok"}, NOW, "complete:bad-state",
                    )
                with sqlite3.connect(store.path) as connection:
                    self.assertEqual("claimed", connection.execute(
                        "SELECT state FROM attempts WHERE attempt_id=?", (claim.attempt_id,)
                    ).fetchone()[0])
                    self.assertEqual(1, connection.execute(
                        "SELECT COUNT(*) FROM conflict_leases WHERE attempt_id=?", (claim.attempt_id,)
                    ).fetchone()[0])
    def test_idempotent_edge_enqueue_does_not_relock_unlocked_successor(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            units = [work_unit(1, priority=20), work_unit(2)]
            edges = [("node-1", "node-2", "required_success")]
            store.enqueue_units("workflow-1", units, edges)
            first = store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            assert first is not None
            store.complete_attempt(
                first.attempt_id, first.claim_token, "succeeded", {"schema_version": 1, "summary": "ok"}, NOW, "done:first"
            )

            self.assertEqual(0, store.enqueue_units("workflow-1", units, edges))
            successor = store.claim_next("torres", {"read_source"}, 124, "start-2", NOW)
            self.assertIsNotNone(successor)

    def test_claim_race_allows_only_one_exclusive_conflict_owner(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            store = RunStore(path)
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            exclusive = (ConflictRequest("repo:example/project", LeaseMode.EXCLUSIVE),)
            store.enqueue_units("workflow-1", [
                work_unit(1, conflicts=exclusive), work_unit(2, conflicts=exclusive),
            ])
            barrier = threading.Barrier(2)
            claims: list[ClaimedWorkUnit | None] = []
            guard = threading.Lock()

            def claim(profile: str, pid: int) -> None:
                local = RunStore(path)
                barrier.wait()
                result = local.claim_next(profile, {"read_source"}, pid, f"start-{pid}", NOW)
                with guard:
                    claims.append(result)

            threads = [
                threading.Thread(target=claim, args=("mcgee", 101)),
                threading.Thread(target=claim, args=("torres", 102)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(1, sum(item is not None for item in claims))

    def test_failed_predecessor_only_unlocks_required_completion_edge(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units(
                "workflow-1", [
                    work_unit(1, priority=20), work_unit(2, priority=10), work_unit(3, priority=10),
                ], [
                    ("node-1", "node-2", "required_completion"),
                    ("node-1", "node-3", "required_success"),
                ],
            )
            claim = store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            assert claim is not None
            store.complete_attempt(
                claim.attempt_id, claim.claim_token, "failed", {"schema_version": 1, "summary": "failed"}, NOW, "failed:node-1"
            )

            next_claim = store.claim_next("torres", {"read_source"}, 124, "start-2", NOW)
            self.assertIsNotNone(next_claim)
            assert next_claim is not None
            self.assertEqual("unit-2", next_claim.work_unit.unit_id)
            self.assertIsNone(store.claim_next("ducky", {"read_source"}, 125, "start-3", NOW))

    def test_json_limits_count_exact_utf8_bytes_before_insert(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            self.assertTrue(store.enqueue_workflow(
                "exact", WorkflowKind.GRAPH, "example/exact", 1, {"x": "a" * 65_528}, NOW
            ))
            with self.assertRaisesRegex(ValueError, "65536 bytes"):
                store.enqueue_workflow(
                    "oversized", WorkflowKind.GRAPH, "example/large", 2,
                    {"x": "a" * 65_529}, NOW,
                )
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(0, connection.execute(
                    "SELECT COUNT(*) FROM workflow_instances WHERE workflow_id='oversized'"
                ).fetchone()[0])

    def test_heartbeat_attempt_requires_current_claim_token(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1)])
            claim = store.claim_next("mcgee", {"read_source"}, 123, "pid-start", NOW)
            assert claim is not None

            later = NOW + timedelta(seconds=30)
            store.heartbeat_attempt(claim.attempt_id, claim.claim_token, later)
            with self.assertRaisesRegex(KeyError, "current claim"):
                store.heartbeat_attempt(claim.attempt_id, "stale-token", later)
            with sqlite3.connect(store.path) as connection:
                heartbeat = connection.execute(
                    "SELECT heartbeat_at FROM attempts WHERE attempt_id=?", (claim.attempt_id,)
                ).fetchone()[0]
            self.assertEqual(later.isoformat(), heartbeat)

    def test_stale_attempt_reconciliation_requires_proven_dead_identity_and_fences_reassignment(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            claims = {}
            for index, profile in enumerate(("mcgee", "torres", "kate"), 1):
                workflow_id = f"workflow-{index}"
                store.enqueue_workflow(
                    workflow_id, WorkflowKind.GRAPH, f"example/project-{index}", index, {}, NOW
                )
                store.enqueue_units(workflow_id, [work_unit(index, workflow_id=workflow_id)])
                claim = store.claim_next(profile, {"read_source"}, 100 + index, f"start-{index}", NOW)
                assert claim is not None
                claims[profile] = claim

            states = {(101, "start-1"): False, (102, "start-2"): True, (103, "start-3"): None}
            reconciled = store.reconcile_stale_attempts(
                NOW + timedelta(minutes=45), lambda pid, start: states[(pid, start)],
                max_attempts=2,
            )
            self.assertEqual([claims["mcgee"].attempt_id], reconciled)
            with sqlite3.connect(store.path) as connection:
                attempt_states = dict(connection.execute(
                    "SELECT profile, state FROM attempts"
                ).fetchall())
            self.assertEqual(
                {"mcgee": "failed", "torres": "claimed", "kate": "claimed"},
                attempt_states,
            )

            replacement = store.claim_next(
                "mcgee", {"read_source"}, 201, "replacement", NOW + timedelta(minutes=46)
            )
            assert replacement is not None
            self.assertEqual(2, replacement.attempt_number)
            self.assertNotEqual(claims["mcgee"].claim_token, replacement.claim_token)
            with self.assertRaisesRegex(KeyError, "current claim"):
                store.heartbeat_attempt(
                    claims["mcgee"].attempt_id, claims["mcgee"].claim_token,
                    NOW + timedelta(minutes=47),
                )
            with self.assertRaisesRegex(KeyError, "current claim"):
                store.complete_attempt(
                    claims["mcgee"].attempt_id, claims["mcgee"].claim_token, "succeeded",
                    {"schema_version": 1, "summary": "late"},
                    NOW + timedelta(minutes=47), "late-completion",
                )

    def test_claim_rejects_node_unit_workflow_and_dependency_divergence(self) -> None:
        corruptions = (
            ("UPDATE graph_nodes SET state='blocked' WHERE node_id='node-2'",),
            ("UPDATE workflow_instances SET state='failed' WHERE workflow_id='workflow-1'",),
            ("UPDATE graph_nodes SET state='failed' WHERE node_id='node-2'",),
            ("UPDATE graph_nodes SET state='cancelled' WHERE node_id='node-2'",),
        )
        for (statement,) in corruptions:
            with self.subTest(statement=statement), TemporaryDirectory() as tmp:
                store = RunStore(Path(tmp) / "runs.sqlite3")
                store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
                store.enqueue_units("workflow-1", [work_unit(1, priority=20), work_unit(2)], [
                    ("node-1", "node-2", "required_success"),
                ])
                with sqlite3.connect(store.path) as connection:
                    authorize_test_transitions(connection)
                    connection.execute("UPDATE work_units SET state='ready' WHERE unit_id='unit-2'")
                    connection.execute(statement)
                claim = store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
                if "workflow_instances" in statement:
                    self.assertIsNone(claim)
                else:
                    self.assertIsNotNone(claim)
                    assert claim is not None
                    self.assertEqual("unit-1", claim.work_unit.unit_id)

    def test_claim_rejects_terminal_predecessor_node_unit_divergence(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units(
                "workflow-1", [work_unit(1, priority=20), work_unit(2)],
                [("node-1", "node-2", "required_success")],
            )
            with sqlite3.connect(store.path) as connection:
                authorize_test_transitions(connection)
                connection.execute("UPDATE graph_nodes SET state='succeeded' WHERE node_id='node-1'")
                connection.execute("UPDATE work_units SET state='failed' WHERE unit_id='unit-1'")
                connection.execute("UPDATE graph_nodes SET state='ready' WHERE node_id='node-2'")
                connection.execute("UPDATE work_units SET state='ready' WHERE unit_id='unit-2'")

            with self.assertRaisesRegex(RuntimeError, "predecessor state divergence"):
                store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])

    def test_dependency_transition_fails_closed_on_node_unit_divergence(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1), work_unit(2)])
            with sqlite3.connect(store.path) as connection:
                authorize_test_transitions(connection)
                connection.execute("UPDATE work_units SET state='blocked' WHERE unit_id='unit-2'")
            with self.assertRaisesRegex(RuntimeError, "state divergence"):
                store.enqueue_units(
                    "workflow-1", [], [("node-1", "node-2", "required_success")]
                )
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(0, connection.execute(
                    "SELECT COUNT(*) FROM graph_edges WHERE successor_node_id='node-2'"
                ).fetchone()[0])

    def test_late_unsatisfied_dependency_terminalizes_active_workflow(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1, priority=20), work_unit(2)])
            first = store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            assert first is not None
            store.complete_attempt(
                first.attempt_id, first.claim_token, "failed",
                {"schema_version": 1, "summary": "failed"}, NOW, "failed:first",
            )
            store.enqueue_units("workflow-1", [], [("node-1", "node-2", "required_success")])
            with sqlite3.connect(store.path) as connection:
                states = connection.execute(
                    "SELECT state FROM graph_nodes WHERE workflow_id='workflow-1' ORDER BY node_id"
                ).fetchall()
                workflow_state = connection.execute(
                    "SELECT state FROM workflow_instances WHERE workflow_id='workflow-1'"
                ).fetchone()[0]
            self.assertEqual([("failed",), ("cancelled",)], states)
            self.assertEqual("failed", workflow_state)

    def test_claim_sqlite_failure_rolls_back_every_transition(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1)])
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    """CREATE TRIGGER inject_claim_failure BEFORE INSERT ON attempts
                    BEGIN SELECT RAISE(ABORT, 'injected claim failure'); END"""
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected claim failure"):
                store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(("queued", "ready", "ready", 0), connection.execute(
                    """SELECT w.state, n.state, u.state, u.current_attempt_number
                       FROM workflow_instances w JOIN graph_nodes n USING(workflow_id)
                       JOIN work_units u USING(workflow_id, node_id)"""
                ).fetchone())
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])

    def test_event_collision_rolls_back_completion_state_and_leases(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1, conflicts=(
                ConflictRequest("repo:example/project", LeaseMode.EXCLUSIVE),
            ))])
            claim = store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            assert claim is not None
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    """INSERT INTO events VALUES
                    ('collision-event','collision-key','workflow-1',NULL,NULL,NULL,
                     'other_event','{}',?)""",
                    (NOW.isoformat(),),
                )
            with self.assertRaisesRegex(ValueError, "idempotency key collision"):
                store.complete_attempt(
                    claim.attempt_id, claim.claim_token, "succeeded",
                    {"schema_version": 1, "summary": "ok"}, NOW, "collision-key",
                )
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(("claimed", "claimed", "claimed"), connection.execute(
                    """SELECT a.state, u.state, n.state FROM attempts a
                       JOIN work_units u USING(unit_id) JOIN graph_nodes n USING(node_id)
                       WHERE a.attempt_id=?""", (claim.attempt_id,),
                ).fetchone())
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(*) FROM conflict_leases WHERE attempt_id=?", (claim.attempt_id,)
                ).fetchone()[0])

    def test_completion_event_failure_rolls_back_terminalization_and_lease_revocation(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1, conflicts=(
                ConflictRequest("repo:example/project", LeaseMode.EXCLUSIVE),
            ))])
            claim = store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            assert claim is not None
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    """CREATE TRIGGER inject_event_failure BEFORE INSERT ON events
                    BEGIN SELECT RAISE(ABORT, 'injected event failure'); END"""
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected event failure"):
                store.complete_attempt(
                    claim.attempt_id, claim.claim_token, "succeeded",
                    {"schema_version": 1, "summary": "ok"}, NOW, "complete:failure",
                )
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(("active", "claimed", "claimed", "claimed"), connection.execute(
                    """SELECT w.state, n.state, u.state, a.state
                       FROM attempts a JOIN work_units u USING(unit_id)
                       JOIN graph_nodes n USING(node_id) JOIN workflow_instances w USING(workflow_id)
                       WHERE a.attempt_id=?""", (claim.attempt_id,),
                ).fetchone())
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(*) FROM conflict_leases WHERE attempt_id=?", (claim.attempt_id,)
                ).fetchone()[0])

    def test_recovery_revalidation_noops_when_completion_wins_race(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            store = RunStore(path)
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1)])
            claim = store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            assert claim is not None
            inspected = threading.Event()
            release = threading.Event()
            recovered: list[list[str]] = []
            errors: list[BaseException] = []

            def recover() -> None:
                try:
                    local = RunStore(path)
                    def dead(_pid: int, _start: str) -> bool:
                        inspected.set()
                        release.wait(timeout=5)
                        return False
                    recovered.append(local.reconcile_stale_attempts(
                        NOW + timedelta(minutes=46), dead, max_attempts=1,
                    ))
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=recover)
            thread.start()
            self.assertTrue(inspected.wait(timeout=5))
            store.complete_attempt(
                claim.attempt_id, claim.claim_token, "succeeded",
                {"schema_version": 1, "summary": "ok"}, NOW + timedelta(minutes=46),
                "completion-wins",
            )
            release.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual([[]], recovered)

    def test_claim_respects_active_v2_profile_reservation(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1)])
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    """INSERT INTO runs(
                        run_id, repo, issue, profile, role, state, created_at, heartbeat_at
                    ) VALUES ('legacy', 'other/project', 9, 'mcgee', 'dev', 'running', ?, ?)""",
                    (NOW.isoformat(), NOW.isoformat()),
                )

            self.assertIsNone(store.claim_next(
                "mcgee", {"read_source"}, 123, "pid-start", NOW
            ))

    def test_claim_next_filters_capabilities_and_reserves_conflicts(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            store.enqueue_workflow(
                "workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW
            )
            store.enqueue_units("workflow-1", [
                work_unit(1, capabilities=("browser",), priority=20),
                work_unit(2, capabilities=("read_source",), conflicts=(
                    ConflictRequest("repo:example/project", LeaseMode.EXCLUSIVE),
                )),
                work_unit(3, capabilities=("read_source",), conflicts=(
                    ConflictRequest("repo:example/project", LeaseMode.SHARED),
                )),
            ])

            claim = store.claim_next("mcgee", {"read_source"}, 123, "pid-start", NOW)
            self.assertIsNotNone(claim)
            assert claim is not None
            self.assertEqual("unit-2", claim.work_unit.unit_id)
            self.assertEqual(1, claim.attempt_number)
            self.assertGreaterEqual(len(claim.claim_token), 32)
            self.assertIsNone(store.claim_next("torres", {"read_source"}, 124, "other", NOW))

    def test_enqueue_workflow_and_units_is_idempotent_and_enforces_total_node_limit(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            self.assertTrue(store.enqueue_workflow(
                "workflow-1", WorkflowKind.GRAPH, "example/project", 5, {"goal": "audit"}, NOW
            ))
            self.assertFalse(store.enqueue_workflow(
                "workflow-1", WorkflowKind.GRAPH, "example/project", 5, {"goal": "audit"}, NOW
            ))
            first = [work_unit(index) for index in range(1, 20)]
            self.assertEqual(19, store.enqueue_units("workflow-1", first))
            self.assertEqual(0, store.enqueue_units("workflow-1", first))
            self.assertEqual(1, store.enqueue_units("workflow-1", [work_unit(20)]))
            with self.assertRaisesRegex(ValueError, "20-node limit"):
                store.enqueue_units("workflow-1", [work_unit(21)])


class MigrationTests(unittest.TestCase):
    def test_v3_to_v4_rejects_orphan_attempt(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            RunStore(path)
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """INSERT INTO attempts VALUES
                    ('orphan-attempt','missing-unit',1,'mcgee','orphan-token','failed',
                     123,'start',?,?,?, '{}')""",
                    (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
                )
                connection.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")

            with self.assertRaisesRegex(sqlite3.IntegrityError, "foreign key integrity"):
                RunStore(path)

    def test_v3_to_v4_rejects_orphan_lease(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            RunStore(path)
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TRIGGER conflict_leases_current_insert")
                connection.execute(
                    "INSERT INTO conflict_leases VALUES ('repo:missing','missing-attempt','shared')"
                )
                connection.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")

            with self.assertRaisesRegex(sqlite3.IntegrityError, "foreign key integrity"):
                RunStore(path)

    def test_v3_to_v4_rejects_nonclaimed_node_unit_state_divergence(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            store = RunStore(path)
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1)])
            with sqlite3.connect(path) as connection:
                authorize_test_transitions(connection)
                connection.execute("UPDATE graph_nodes SET state='blocked' WHERE node_id='node-1'")
                connection.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")

            with self.assertRaisesRegex(sqlite3.IntegrityError, "state coherence"):
                RunStore(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual("3", connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0])

    def test_reopening_v4_rejects_state_divergence(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            store = RunStore(path)
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1)])
            with sqlite3.connect(path) as connection:
                authorize_test_transitions(connection)
                connection.execute("UPDATE graph_nodes SET state='blocked' WHERE node_id='node-1'")

            with self.assertRaisesRegex(sqlite3.IntegrityError, "state coherence"):
                RunStore(path)

    def test_v3_to_v4_rejects_two_exclusive_leases_for_one_domain(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            store = RunStore(path)
            for index, profile in ((1, "mcgee"), (2, "torres")):
                workflow_id = f"workflow-{index}"
                store.enqueue_workflow(
                    workflow_id, WorkflowKind.GRAPH, f"example/project-{index}", index, {}, NOW
                )
                store.enqueue_units(workflow_id, [work_unit(
                    index, workflow_id=workflow_id,
                    conflicts=(ConflictRequest(f"domain-{index}", LeaseMode.EXCLUSIVE),),
                )])
                self.assertIsNotNone(store.claim_next(
                    profile, {"read_source"}, 120 + index, f"start-{index}", NOW
                ))
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TRIGGER conflict_leases_current_update")
                connection.execute("UPDATE conflict_leases SET domain='same-domain'")
                connection.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")

            with self.assertRaisesRegex(sqlite3.IntegrityError, "lease compatibility"):
                RunStore(path)

    def test_v3_to_v4_rejects_shared_and_exclusive_leases_for_one_domain(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            store = RunStore(path)
            for index, (profile, mode) in enumerate(
                (("mcgee", LeaseMode.SHARED), ("torres", LeaseMode.EXCLUSIVE)), 1
            ):
                workflow_id = f"workflow-{index}"
                store.enqueue_workflow(
                    workflow_id, WorkflowKind.GRAPH, f"example/project-{index}", index, {}, NOW
                )
                store.enqueue_units(workflow_id, [work_unit(
                    index,
                    workflow_id=workflow_id,
                    conflicts=(ConflictRequest(f"domain-{index}", mode),),
                )])
                self.assertIsNotNone(store.claim_next(
                    profile, {"read_source"}, 120 + index, f"start-{index}", NOW
                ))
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TRIGGER conflict_leases_current_update")
                connection.execute("UPDATE conflict_leases SET domain='same-domain'")
                connection.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")

            with self.assertRaisesRegex(sqlite3.IntegrityError, "lease compatibility"):
                RunStore(path)

    def test_v3_to_v4_migration_preserves_data_and_rejects_invalid_leases_atomically(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            store = RunStore(path)
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1)])
            with sqlite3.connect(path) as connection:
                for name in (
                    "conflict_leases_current_insert", "conflict_leases_current_update",
                    "attempts_no_terminal_leases", "work_units_no_terminal_leases",
                ):
                    connection.execute(f"DROP TRIGGER {name}")
                connection.execute("DROP INDEX one_claimed_attempt_per_unit")
                connection.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")
                connection.execute(
                    """INSERT INTO attempts VALUES
                    ('terminal-attempt','unit-1',1,'mcgee','terminal-token','failed',
                     123,'start',?,?,?, '{}')""",
                    (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
                )
                connection.execute(
                    "INSERT INTO conflict_leases VALUES ('repo:example/project','terminal-attempt','exclusive')"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid conflict lease"):
                RunStore(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual("3", connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0])
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(*) FROM conflict_leases WHERE attempt_id='terminal-attempt'"
                ).fetchone()[0])
                connection.execute("DELETE FROM conflict_leases")
            RunStore(path)
            RunStore(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual("4", connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0])
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(*) FROM attempts WHERE attempt_id='terminal-attempt'"
                ).fetchone()[0])

    def test_v4_enforces_one_claimed_attempt_and_current_lease_ownership(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            store = RunStore(path)
            store.enqueue_workflow("workflow-1", WorkflowKind.GRAPH, "example/project", 5, {}, NOW)
            store.enqueue_units("workflow-1", [work_unit(1, conflicts=(
                ConflictRequest("repo:example/project", LeaseMode.EXCLUSIVE),
            ))])
            claim = store.claim_next("mcgee", {"read_source"}, 123, "start", NOW)
            assert claim is not None
            with sqlite3.connect(path) as connection:
                self.assertEqual("4", connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0])
                with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE"):
                    connection.execute(
                        """INSERT INTO attempts VALUES
                        ('attempt-conflict','unit-1',2,'torres','token-conflict','claimed',
                         124,'start-2',?,?,NULL,NULL)""",
                        (NOW.isoformat(), NOW.isoformat()),
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "current claimed attempt"):
                    connection.execute(
                        "INSERT INTO conflict_leases VALUES ('bad','missing','exclusive')"
                    )
                authorize_test_transitions(connection)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "active conflict lease"):
                    connection.execute(
                        "UPDATE attempts SET state='failed' WHERE attempt_id=?", (claim.attempt_id,)
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "active conflict lease"):
                    connection.execute("UPDATE work_units SET state='failed' WHERE unit_id='unit-1'")

    def test_existing_v2_database_migrates_to_v4_idempotently_with_provenance_guards(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE runs (
                        run_id TEXT PRIMARY KEY, repo TEXT NOT NULL, issue INTEGER NOT NULL,
                        profile TEXT NOT NULL, role TEXT NOT NULL, state TEXT NOT NULL,
                        created_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, pid INTEGER,
                        process_start TEXT, outcome TEXT, result_path TEXT, workspace_json TEXT
                    );
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO metadata VALUES ('schema_version', '2');
                    """
                )
                connection.row_factory = sqlite3.Row
                RunStore._migrate_v2(connection)
                connection.execute(
                    """INSERT INTO workflow_instances VALUES
                    ('workflow-1','graph','example/project',5,'queued','{}',?,?)""",
                    (NOW.isoformat(), NOW.isoformat()),
                )

            RunStore(path)
            RunStore(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual("4", connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0])
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(*) FROM workflow_instances WHERE workflow_id='workflow-1'"
                ).fetchone()[0])
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(
                    """INSERT INTO workflow_instances VALUES
                    ('workflow-2','graph','example/other',6,'queued','{}',?,?)""",
                    (NOW.isoformat(), NOW.isoformat()),
                )
                connection.execute(
                    """INSERT INTO graph_nodes VALUES
                    ('node-2','workflow-2','node-2','agent','ready','[\"read_source\"]',
                     NULL,'{}',?,?)""",
                    (NOW.isoformat(), NOW.isoformat()),
                )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "provenance"):
                    connection.execute(
                        """INSERT INTO work_units VALUES
                        ('unit-bad','workflow-1','node-2','ready',NULL,'[\"read_source\"]',
                         '[]','{}',1,?,?,0)""",
                        (NOW.isoformat(), NOW.isoformat()),
                    )

    def test_schema_validation_precedes_mutation_and_preserves_journal_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "future.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("INSERT INTO metadata VALUES ('schema_version', '99')")
            with self.assertRaisesRegex(RuntimeError, "unsupported database schema version: 99"):
                RunStore(path)
            with self.assertRaisesRegex(RuntimeError, "unsupported database schema version: 99"):
                RunStore(path, read_only=True)
            with sqlite3.connect(path) as connection:
                self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])

            supported = Path(tmp) / "supported.sqlite3"
            store = RunStore(supported)
            with sqlite3.connect(supported) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
            RunStore(supported)
            with sqlite3.connect(supported) as connection:
                self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            store.close()

    def test_unknown_newer_schema_is_rejected_without_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata VALUES ('schema_version', '99');
                CREATE TABLE sentinel (value TEXT);
                INSERT INTO sentinel VALUES ('preserved');
                """
            )
            connection.close()

            with self.assertRaisesRegex(RuntimeError, "unsupported database schema version: 99"):
                RunStore(path)
            with sqlite3.connect(path) as unchanged:
                self.assertEqual("99", unchanged.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0])
                self.assertEqual("preserved", unchanged.execute(
                    "SELECT value FROM sentinel"
                ).fetchone()[0])

    def test_existing_v1_database_migrates_additively_to_v4(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY, repo TEXT NOT NULL, issue INTEGER NOT NULL,
                    profile TEXT NOT NULL, role TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, pid INTEGER,
                    process_start TEXT, outcome TEXT, result_path TEXT, workspace_json TEXT
                );
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata VALUES ('schema_version', '1');
                INSERT INTO runs(run_id, repo, issue, profile, role, state, created_at, heartbeat_at)
                VALUES ('old', 'example/project', 7, 'mcgee', 'dev', 'prepared', 't', 't');
                """
            )
            connection.close()

            store = RunStore(path)
            self.assertEqual("old", store.get_run("old")["run_id"])
            with sqlite3.connect(path) as migrated:
                version = migrated.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in migrated.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual("4", version)
            self.assertTrue(
                {
                    "workflow_instances", "graph_nodes", "graph_edges", "work_units",
                    "attempts", "conflict_leases", "artifacts", "events", "scheduler_state",
                }.issubset(tables)
            )


class ModelTests(unittest.TestCase):
    def test_v3_enums_and_dtos_are_backward_compatible_additions(self) -> None:
        conflict = ConflictRequest("repo:example/project", LeaseMode.SHARED)
        unit = WorkUnit(
            unit_id="unit-1",
            workflow_id="workflow-1",
            node_id="node-1",
            workflow_kind=WorkflowKind.GRAPH,
            node_type=NodeType.AGENT,
            profile="mcgee",
            capabilities=("read_source",),
            conflicts=(conflict,),
            payload={"prompt": "audit"},
            priority=10,
            created_at=NOW,
        )
        claimed = ClaimedWorkUnit(unit, "attempt-1", 1, "opaque-token")

        self.assertEqual("single", WorkflowKind.SINGLE.value)
        self.assertEqual("human_gate", NodeType.HUMAN_GATE.value)
        self.assertEqual("exclusive", LeaseMode.EXCLUSIVE.value)
        self.assertEqual("unit-1", claimed.work_unit.unit_id)
        self.assertEqual(1, claimed.attempt_number)


if __name__ == "__main__":
    unittest.main()
