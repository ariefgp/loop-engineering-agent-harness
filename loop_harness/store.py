from __future__ import annotations

import os
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from .models import (
    ClaimedWorkUnit,
    ConflictRequest,
    LeaseMode,
    NodeType,
    WorkUnit,
    WorkflowKind,
)
from .scheduler import Assignment


_ACTIVE = ("prepared", "running")
_STALE_AFTER = timedelta(minutes=45)
_MAX_NODES = 20
_MAX_JSON_BYTES = 65_536
_SCHEMA_VERSION = 4


class _ClosingConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3.Connection, then deterministically close."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class RunStore:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        if path.is_file():
            self._validate_existing_schema()
        if read_only:
            if not path.is_file():
                raise FileNotFoundError(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        self._initialize()

    def close(self) -> None:
        return None

    def _validate_existing_schema(self) -> None:
        """Reject unsupported schemas using a non-mutating connection."""
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro",
            timeout=10,
            isolation_level=None,
            uri=True,
        )
        try:
            try:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table: metadata" in str(exc):
                    return
                raise
            if row is None:
                return
            try:
                version = int(row[0])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("invalid database schema version") from exc
            if version < 1 or version > _SCHEMA_VERSION:
                raise RuntimeError(f"unsupported database schema version: {version}")
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                timeout=10,
                isolation_level=None,
                uri=True,
                factory=_ClosingConnection,
            )
        else:
            connection = sqlite3.connect(
                self.path, timeout=10, isolation_level=None, factory=_ClosingConnection
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection._runstore_transition_authorized = False  # type: ignore[attr-defined]
        connection.create_function(
            "runstore_transition_authorized",
            0,
            lambda: int(connection._runstore_transition_authorized),  # type: ignore[attr-defined]
        )
        if self.read_only:
            connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    @contextmanager
    def _authorized_transitions(connection: sqlite3.Connection):
        if getattr(connection, "_runstore_transition_authorized", False):
            raise RuntimeError("nested state transition authorization")
        connection._runstore_transition_authorized = True  # type: ignore[attr-defined]
        try:
            yield
        finally:
            connection._runstore_transition_authorized = False  # type: ignore[attr-defined]

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                version_row = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                if version_row is None:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS runs (
                            run_id TEXT PRIMARY KEY,
                            repo TEXT NOT NULL,
                            issue INTEGER NOT NULL,
                            profile TEXT NOT NULL,
                            role TEXT NOT NULL,
                            state TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            heartbeat_at TEXT NOT NULL,
                            pid INTEGER,
                            process_start TEXT,
                            outcome TEXT,
                            result_path TEXT,
                            workspace_json TEXT
                        )
                        """
                    )
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES ('schema_version', '1')"
                    )
                    version = 1
                else:
                    try:
                        version = int(version_row["value"])
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError("invalid database schema version") from exc
                if version > _SCHEMA_VERSION:
                    raise RuntimeError(f"unsupported database schema version: {version}")
                if version < 1:
                    raise RuntimeError(f"unsupported database schema version: {version}")

                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(runs)").fetchall()
                }
                if version == 1:
                    if "workspace_json" not in columns:
                        connection.execute("ALTER TABLE runs ADD COLUMN workspace_json TEXT")
                    self._migrate_v2(connection)
                    connection.execute(
                        "UPDATE metadata SET value='2' WHERE key='schema_version'"
                    )
                    version = 2
                if version == 2:
                    self._migrate_v3(connection)
                    connection.execute(
                        "UPDATE metadata SET value='3' WHERE key='schema_version'"
                    )
                    version = 3
                if version == 3:
                    self._migrate_v4(connection)
                    connection.execute(
                        "UPDATE metadata SET value='4' WHERE key='schema_version'"
                    )
                    version = 4
                elif version == 4:
                    self._migrate_v4(connection)
                connection.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS one_active_issue
                    ON runs(repo, issue) WHERE state IN ('prepared', 'running')"""
                )
                connection.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS one_active_profile
                    ON runs(profile) WHERE state IN ('prepared', 'running')"""
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _migrate_v2(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE workflow_instances (
                workflow_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('single','graph')),
                repo TEXT NOT NULL,
                issue INTEGER NOT NULL CHECK(issue > 0),
                state TEXT NOT NULL CHECK(state IN ('queued','active','succeeded','failed','cancelled')),
                input_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE UNIQUE INDEX one_active_workflow_issue
                ON workflow_instances(repo, issue)
                WHERE state IN ('queued','active')""",
            """CREATE TABLE graph_nodes (
                node_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL REFERENCES workflow_instances(workflow_id) ON DELETE CASCADE,
                node_key TEXT NOT NULL,
                node_type TEXT NOT NULL CHECK(node_type IN ('deterministic','agent','verifier','reducer','human_gate','integrator')),
                state TEXT NOT NULL CHECK(state IN ('blocked','ready','claimed','succeeded','failed','cancelled')),
                required_capabilities_json TEXT NOT NULL,
                profile TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workflow_id, node_key),
                UNIQUE(workflow_id, node_id)
            )""",
            """CREATE TABLE graph_edges (
                workflow_id TEXT NOT NULL REFERENCES workflow_instances(workflow_id) ON DELETE CASCADE,
                predecessor_node_id TEXT NOT NULL,
                successor_node_id TEXT NOT NULL,
                requirement TEXT NOT NULL CHECK(requirement IN ('required_success','required_completion')),
                PRIMARY KEY(workflow_id, predecessor_node_id, successor_node_id),
                FOREIGN KEY(workflow_id, predecessor_node_id) REFERENCES graph_nodes(workflow_id, node_id) ON DELETE CASCADE,
                FOREIGN KEY(workflow_id, successor_node_id) REFERENCES graph_nodes(workflow_id, node_id) ON DELETE CASCADE,
                CHECK(predecessor_node_id <> successor_node_id)
            )""",
            """CREATE INDEX graph_edges_successor ON graph_edges(workflow_id, successor_node_id)""",
            """CREATE TABLE work_units (
                unit_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL REFERENCES workflow_instances(workflow_id) ON DELETE CASCADE,
                node_id TEXT NOT NULL UNIQUE REFERENCES graph_nodes(node_id) ON DELETE CASCADE,
                state TEXT NOT NULL CHECK(state IN ('blocked','ready','claimed','succeeded','failed','cancelled')),
                profile TEXT,
                capabilities_json TEXT NOT NULL,
                conflicts_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                priority INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                current_attempt_number INTEGER NOT NULL DEFAULT 0 CHECK(current_attempt_number >= 0)
            )""",
            """CREATE INDEX work_units_claim_order ON work_units(state, priority DESC, available_at, created_at, unit_id)""",
            """CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY,
                unit_id TEXT NOT NULL REFERENCES work_units(unit_id) ON DELETE CASCADE,
                attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
                profile TEXT NOT NULL,
                claim_token TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('claimed','succeeded','failed')),
                runner_pid INTEGER NOT NULL CHECK(runner_pid > 0),
                runner_start TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                completed_at TEXT,
                result_json TEXT,
                UNIQUE(unit_id, attempt_number)
            )""",
            """CREATE UNIQUE INDEX one_active_v3_profile ON attempts(profile) WHERE state='claimed'""",
            """CREATE TABLE conflict_leases (
                domain TEXT NOT NULL,
                attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
                mode TEXT NOT NULL CHECK(mode IN ('shared','exclusive')),
                PRIMARY KEY(domain, attempt_id)
            )""",
            """CREATE INDEX conflict_leases_domain ON conflict_leases(domain, mode)""",
            """CREATE TABLE artifacts (
                artifact_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL REFERENCES workflow_instances(workflow_id) ON DELETE CASCADE,
                node_id TEXT NOT NULL REFERENCES graph_nodes(node_id) ON DELETE CASCADE,
                attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
                artifact_type TEXT NOT NULL,
                schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                source_revision TEXT NOT NULL,
                sha256 TEXT NOT NULL CHECK(length(sha256)=64),
                summary_json TEXT NOT NULL,
                location TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE INDEX artifacts_node ON artifacts(workflow_id, node_id)""",
            """CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                workflow_id TEXT NOT NULL REFERENCES workflow_instances(workflow_id) ON DELETE CASCADE,
                node_id TEXT REFERENCES graph_nodes(node_id) ON DELETE CASCADE,
                unit_id TEXT REFERENCES work_units(unit_id) ON DELETE CASCADE,
                attempt_id TEXT REFERENCES attempts(attempt_id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE INDEX events_workflow_order ON events(workflow_id, created_at, event_id)""",
            """CREATE TABLE scheduler_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
        )
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migrate_v3(connection: sqlite3.Connection) -> None:
        work_unit_guard = """
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM graph_nodes n
                WHERE n.node_id=NEW.node_id AND n.workflow_id=NEW.workflow_id
            ) THEN RAISE(ABORT, 'work unit provenance mismatch') END;
        """
        artifact_guard = """
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1
                FROM attempts a
                JOIN work_units u ON u.unit_id=a.unit_id
                WHERE a.attempt_id=NEW.attempt_id
                  AND u.workflow_id=NEW.workflow_id
                  AND u.node_id=NEW.node_id
            ) THEN RAISE(ABORT, 'artifact provenance mismatch') END;
        """
        event_guard = """
            SELECT CASE WHEN NEW.node_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM graph_nodes n
                WHERE n.node_id=NEW.node_id AND n.workflow_id=NEW.workflow_id
            ) THEN RAISE(ABORT, 'event node provenance mismatch') END;
            SELECT CASE WHEN NEW.unit_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM work_units u
                WHERE u.unit_id=NEW.unit_id AND u.workflow_id=NEW.workflow_id
                  AND (NEW.node_id IS NULL OR u.node_id=NEW.node_id)
            ) THEN RAISE(ABORT, 'event unit provenance mismatch') END;
            SELECT CASE WHEN NEW.attempt_id IS NOT NULL AND NOT EXISTS (
                SELECT 1
                FROM attempts a
                JOIN work_units u ON u.unit_id=a.unit_id
                WHERE a.attempt_id=NEW.attempt_id
                  AND u.workflow_id=NEW.workflow_id
                  AND (NEW.unit_id IS NULL OR u.unit_id=NEW.unit_id)
                  AND (NEW.node_id IS NULL OR u.node_id=NEW.node_id)
            ) THEN RAISE(ABORT, 'event attempt provenance mismatch') END;
        """
        guards = {
            "work_units_provenance": ("work_units", work_unit_guard),
            "artifacts_provenance": ("artifacts", artifact_guard),
            "events_provenance": ("events", event_guard),
        }
        for name, (table, body) in guards.items():
            for operation in ("INSERT", "UPDATE"):
                connection.execute(
                    f"""CREATE TRIGGER IF NOT EXISTS {name}_{operation.lower()}
                    BEFORE {operation} ON {table}
                    BEGIN {body} END"""
                )

    @staticmethod
    def _migrate_v4(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError("foreign key integrity check failed")
        invalid_lease = connection.execute(
            """SELECT 1
               FROM conflict_leases l
               LEFT JOIN attempts a ON a.attempt_id=l.attempt_id
               LEFT JOIN work_units u ON u.unit_id=a.unit_id
               LEFT JOIN graph_nodes n ON n.node_id=u.node_id AND n.workflow_id=u.workflow_id
               LEFT JOIN workflow_instances w ON w.workflow_id=u.workflow_id
               WHERE a.attempt_id IS NULL OR a.state<>'claimed'
                  OR u.state<>'claimed' OR n.state<>'claimed' OR w.state<>'active'
                  OR a.attempt_number<>u.current_attempt_number
               LIMIT 1"""
        ).fetchone()
        if invalid_lease is not None:
            raise sqlite3.IntegrityError("invalid conflict lease ownership")
        incompatible_lease = connection.execute(
            """SELECT domain FROM conflict_leases
               GROUP BY domain
               HAVING SUM(mode='exclusive') > 1
                  OR (SUM(mode='exclusive') > 0 AND COUNT(*) > 1)
               LIMIT 1"""
        ).fetchone()
        if incompatible_lease is not None:
            raise sqlite3.IntegrityError("invalid conflict lease compatibility")
        invalid_claim = connection.execute(
            """SELECT 1
               FROM work_units u
               JOIN graph_nodes n ON n.node_id=u.node_id AND n.workflow_id=u.workflow_id
               JOIN workflow_instances w ON w.workflow_id=u.workflow_id
               WHERE u.state<>n.state
                  OR (u.state='claimed')<>(
                      w.state='active' AND 1=(
                          SELECT COUNT(*) FROM attempts a
                          WHERE a.unit_id=u.unit_id AND a.state='claimed'
                            AND a.attempt_number=u.current_attempt_number
                      )
                  )
               LIMIT 1"""
        ).fetchone()
        invalid_attempt = connection.execute(
            """SELECT 1
               FROM attempts a
               LEFT JOIN work_units u ON u.unit_id=a.unit_id
               LEFT JOIN graph_nodes n ON n.node_id=u.node_id AND n.workflow_id=u.workflow_id
               LEFT JOIN workflow_instances w ON w.workflow_id=u.workflow_id
               WHERE u.unit_id IS NULL
                  OR (a.state='claimed' AND (
                   n.node_id IS NULL OR w.workflow_id IS NULL
                   OR u.state<>'claimed' OR n.state<>'claimed' OR w.state<>'active'
                   OR a.attempt_number<>u.current_attempt_number
               )) LIMIT 1"""
        ).fetchone()
        if invalid_claim is not None or invalid_attempt is not None:
            raise sqlite3.IntegrityError("invalid claimed state coherence")
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS one_claimed_attempt_per_unit
               ON attempts(unit_id) WHERE state='claimed'"""
        )
        lease_guard = """
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1
                FROM attempts a
                JOIN work_units u ON u.unit_id=a.unit_id
                JOIN graph_nodes n ON n.node_id=u.node_id AND n.workflow_id=u.workflow_id
                JOIN workflow_instances w ON w.workflow_id=u.workflow_id
                WHERE a.attempt_id=NEW.attempt_id
                  AND a.state='claimed' AND u.state='claimed' AND n.state='claimed'
                  AND w.state='active'
                  AND a.attempt_number=u.current_attempt_number
            ) THEN RAISE(ABORT, 'lease requires current claimed attempt') END;
        """
        for operation in ("INSERT", "UPDATE"):
            compatibility = (
                """SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM conflict_leases l
                    WHERE l.domain=NEW.domain
                      AND (NEW.mode='exclusive' OR l.mode='exclusive')
                ) THEN RAISE(ABORT, 'incompatible conflict lease') END;"""
                if operation == "INSERT"
                else """SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM conflict_leases l
                    WHERE l.domain=NEW.domain AND l.rowid<>OLD.rowid
                      AND (NEW.mode='exclusive' OR l.mode='exclusive')
                ) THEN RAISE(ABORT, 'incompatible conflict lease') END;"""
            )
            connection.execute(
                f"DROP TRIGGER IF EXISTS conflict_leases_current_{operation.lower()}"
            )
            connection.execute(
                f"""CREATE TRIGGER conflict_leases_current_{operation.lower()}
                BEFORE {operation} ON conflict_leases
                BEGIN {lease_guard} {compatibility} END"""
            )
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS attempts_no_terminal_leases
            BEFORE UPDATE OF state ON attempts
            WHEN OLD.state='claimed' AND NEW.state<>'claimed'
            BEGIN
                SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM conflict_leases l WHERE l.attempt_id=OLD.attempt_id
                ) THEN RAISE(ABORT, 'attempt has active conflict lease') END;
            END"""
        )
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS work_units_no_terminal_leases
            BEFORE UPDATE OF state ON work_units
            WHEN OLD.state='claimed' AND NEW.state<>'claimed'
            BEGIN
                SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM conflict_leases l
                    JOIN attempts a ON a.attempt_id=l.attempt_id
                    WHERE a.unit_id=OLD.unit_id
                ) THEN RAISE(ABORT, 'work unit has active conflict lease') END;
            END"""
        )
        for table in ("graph_nodes", "work_units", "attempts"):
            trigger = f"{table}_state_transition_authorized"
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            connection.execute(
                f"""CREATE TRIGGER {trigger}
                BEFORE UPDATE OF state ON {table}
                WHEN OLD.state<>NEW.state
                BEGIN
                    SELECT CASE WHEN runstore_transition_authorized()<>1
                        THEN RAISE(ABORT, 'unauthorized state transition') END;
                END"""
            )

    @staticmethod
    def _required_text(value: object, *, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a nonempty string")
        return value

    @staticmethod
    def _utc_timestamp(value: datetime, *, field: str) -> str:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _bounded_json(value: object, *, field: str) -> str:
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be JSON serializable") from exc
        if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
            raise ValueError(f"{field} exceeds {_MAX_JSON_BYTES} bytes")
        return encoded

    def enqueue_workflow(
        self,
        workflow_id: str,
        kind: WorkflowKind,
        repo: str,
        issue: int,
        input_data: dict[str, object],
        at: datetime,
    ) -> bool:
        self._required_text(workflow_id, field="workflow id")
        self._required_text(repo, field="repository")
        if not isinstance(kind, WorkflowKind):
            raise ValueError("workflow kind is invalid")
        if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
            raise ValueError("issue must be a positive integer")
        if not isinstance(input_data, dict):
            raise ValueError("workflow input must be a JSON object")
        encoded = self._bounded_json(input_data, field="workflow input")
        timestamp = self._utc_timestamp(at, field="workflow timestamp")
        values = (workflow_id, kind.value, repo, issue, "queued", encoded, timestamp, timestamp)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT kind, repo, issue, input_json FROM workflow_instances WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if existing is not None:
                if (existing["kind"], existing["repo"], existing["issue"], existing["input_json"]) != (
                    kind.value, repo, issue, encoded
                ):
                    raise ValueError(f"workflow id collision: {workflow_id}")
                connection.commit()
                return False
            connection.execute(
                """INSERT INTO workflow_instances(
                    workflow_id, kind, repo, issue, state, input_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            connection.commit()
            return True

    def enqueue_units(
        self,
        workflow_id: str,
        units: Iterable[WorkUnit],
        edges: Iterable[tuple[str, str, str]] = (),
    ) -> int:
        pending = list(units)
        edge_list = list(edges)
        self._required_text(workflow_id, field="workflow id")
        if any(unit.workflow_id != workflow_id for unit in pending):
            raise ValueError("work unit belongs to another workflow")
        if len({unit.node_id for unit in pending}) != len(pending) or len(
            {unit.unit_id for unit in pending}
        ) != len(pending):
            raise ValueError("duplicate work unit or node id")
        encoded: list[tuple[WorkUnit, str, str, str, str]] = []
        for unit in pending:
            self._required_text(unit.unit_id, field="work unit id")
            self._required_text(unit.node_id, field="node id")
            if unit.profile is not None:
                self._required_text(unit.profile, field="profile")
            if not unit.capabilities or any(
                not isinstance(item, str) or not item.strip() for item in unit.capabilities
            ):
                raise ValueError("capabilities must contain nonempty strings")
            if len(set(unit.capabilities)) != len(unit.capabilities):
                raise ValueError("capabilities must be unique")
            if not isinstance(unit.payload, dict):
                raise ValueError("work unit payload must be a JSON object")
            seen_domains: dict[str, LeaseMode] = {}
            for conflict in unit.conflicts:
                domain = self._required_text(conflict.domain, field="conflict domain")
                if not isinstance(conflict.mode, LeaseMode):
                    raise ValueError("conflict mode is invalid")
                if domain in seen_domains:
                    if seen_domains[domain] is not conflict.mode:
                        raise ValueError(f"conflicting duplicate conflict domain: {domain}")
                    raise ValueError(f"duplicate conflict domain: {domain}")
                seen_domains[domain] = conflict.mode
            capabilities = self._bounded_json(sorted(unit.capabilities), field="capabilities")
            conflicts = self._bounded_json(
                [
                    {"domain": domain, "mode": seen_domains[domain].value}
                    for domain in sorted(seen_domains)
                ],
                field="conflicts",
            )
            payload = self._bounded_json(unit.payload, field="work unit payload")
            timestamp = self._utc_timestamp(unit.created_at, field="work unit timestamp")
            encoded.append((unit, capabilities, conflicts, payload, timestamp))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow = connection.execute(
                "SELECT kind, state FROM workflow_instances WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                raise KeyError(f"workflow not found: {workflow_id}")
            if workflow["state"] in ("succeeded", "failed", "cancelled"):
                raise ValueError(f"cannot add units to terminal workflow: {workflow_id}")
            if any(item[0].workflow_kind.value != workflow["kind"] for item in encoded):
                raise ValueError("work unit kind does not match workflow kind")
            existing_count = int(connection.execute(
                "SELECT COUNT(*) FROM graph_nodes WHERE workflow_id=?", (workflow_id,)
            ).fetchone()[0])
            new_nodes = [
                item for item in encoded
                if connection.execute("SELECT 1 FROM graph_nodes WHERE node_id=?", (item[0].node_id,)).fetchone()
                is None
            ]
            if existing_count + len(new_nodes) > _MAX_NODES:
                raise ValueError("workflow exceeds 20-node limit")

            inserted = 0
            now = encoded[0][4] if encoded else self._utc_timestamp(
                datetime.now(UTC), field="work unit timestamp"
            )
            for unit, capabilities, conflicts, payload, timestamp in encoded:
                prior = connection.execute(
                    """SELECT n.workflow_id, n.node_type, n.profile, n.required_capabilities_json,
                              n.payload_json, u.unit_id, u.conflicts_json, u.priority
                       FROM graph_nodes n JOIN work_units u ON u.node_id=n.node_id
                       WHERE n.node_id=?""",
                    (unit.node_id,),
                ).fetchone()
                expected = (
                    workflow_id, unit.node_type.value, unit.profile, capabilities,
                    payload, unit.unit_id, conflicts, unit.priority,
                )
                if prior is not None:
                    actual = tuple(prior[key] for key in (
                        "workflow_id", "node_type", "profile", "required_capabilities_json",
                        "payload_json", "unit_id", "conflicts_json", "priority",
                    ))
                    if actual != expected:
                        raise ValueError(f"node id collision: {unit.node_id}")
                    continue
                connection.execute(
                    """INSERT INTO graph_nodes(
                        node_id, workflow_id, node_key, node_type, state,
                        required_capabilities_json, profile, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?)""",
                    (unit.node_id, workflow_id, unit.node_id, unit.node_type.value,
                     capabilities, unit.profile, payload, timestamp, timestamp),
                )
                connection.execute(
                    """INSERT INTO work_units(
                        unit_id, workflow_id, node_id, state, profile, capabilities_json,
                        conflicts_json, payload_json, priority, created_at, available_at
                    ) VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?)""",
                    (unit.unit_id, workflow_id, unit.node_id, unit.profile, capabilities,
                     conflicts, payload, unit.priority, timestamp, timestamp),
                )
                inserted += 1
            inserted_successors: list[str] = []
            for predecessor, successor, requirement in edge_list:
                self._required_text(predecessor, field="predecessor node id")
                self._required_text(successor, field="successor node id")
                if requirement not in ("required_success", "required_completion"):
                    raise ValueError(f"invalid edge requirement: {requirement}")
                prior_edge = connection.execute(
                    """SELECT requirement FROM graph_edges
                       WHERE workflow_id=? AND predecessor_node_id=? AND successor_node_id=?""",
                    (workflow_id, predecessor, successor),
                ).fetchone()
                if prior_edge is not None:
                    if prior_edge["requirement"] != requirement:
                        raise ValueError(
                            f"edge requirement collision: {predecessor}->{successor}"
                        )
                    continue
                successor_row = connection.execute(
                    "SELECT state FROM graph_nodes WHERE workflow_id=? AND node_id=?",
                    (workflow_id, successor),
                ).fetchone()
                if successor_row is None:
                    raise ValueError(f"successor node not found: {successor}")
                if successor_row["state"] not in ("ready", "blocked"):
                    raise ValueError(
                        f"cannot add prerequisite to successor in {successor_row['state']} state"
                    )
                connection.execute(
                    """INSERT INTO graph_edges(
                        workflow_id, predecessor_node_id, successor_node_id, requirement
                    ) VALUES (?, ?, ?, ?)""",
                    (workflow_id, predecessor, successor, requirement),
                )
                inserted_successors.append(successor)
            self._validate_graph(connection, workflow_id)
            with self._authorized_transitions(connection):
                for successor in dict.fromkeys(inserted_successors):
                    self._evaluate_node(connection, workflow_id, successor, now)
                if workflow["state"] == "active":
                    self._terminalize_workflow_if_complete(connection, workflow_id, now)
            connection.commit()
            return inserted

    @staticmethod
    def _validate_graph(connection: sqlite3.Connection, workflow_id: str) -> None:
        rows = connection.execute(
            """SELECT predecessor_node_id, successor_node_id
               FROM graph_edges WHERE workflow_id=?""",
            (workflow_id,),
        ).fetchall()
        adjacency: dict[str, list[str]] = {}
        nodes: set[str] = set()
        for row in rows:
            predecessor = str(row["predecessor_node_id"])
            successor = str(row["successor_node_id"])
            adjacency.setdefault(predecessor, []).append(successor)
            nodes.update((predecessor, successor))
        visiting: set[str] = set()
        depths: dict[str, int] = {}

        def depth(node_id: str) -> int:
            if node_id in visiting:
                raise ValueError("workflow graph contains a cycle")
            if node_id in depths:
                return depths[node_id]
            visiting.add(node_id)
            value = max((1 + depth(item) for item in adjacency.get(node_id, ())), default=0)
            visiting.remove(node_id)
            depths[node_id] = value
            return value

        if max((depth(node_id) for node_id in nodes), default=0) > 5:
            raise ValueError("workflow graph exceeds maximum depth 5")

    @staticmethod
    def _evaluate_node(
        connection: sqlite3.Connection, workflow_id: str, node_id: str, timestamp: str
    ) -> None:
        node = connection.execute(
            """SELECT n.state AS node_state, u.state AS unit_state
               FROM graph_nodes n
               JOIN work_units u ON u.workflow_id=n.workflow_id AND u.node_id=n.node_id
               WHERE n.workflow_id=? AND n.node_id=?""",
            (workflow_id, node_id),
        ).fetchone()
        if node is None:
            return
        if node["node_state"] != node["unit_state"]:
            raise RuntimeError(f"node/work unit state divergence: {node_id}")
        if node["node_state"] not in ("ready", "blocked"):
            return
        predecessors = connection.execute(
            """SELECT e.requirement, n.state FROM graph_edges e
               JOIN graph_nodes n
                 ON n.workflow_id=e.workflow_id AND n.node_id=e.predecessor_node_id
               WHERE e.workflow_id=? AND e.successor_node_id=?""",
            (workflow_id, node_id),
        ).fetchall()
        terminal = ("succeeded", "failed", "cancelled")
        impossible = any(
            row["requirement"] == "required_success"
            and row["state"] in terminal
            and row["state"] != "succeeded"
            for row in predecessors
        )
        if impossible:
            node_cursor = connection.execute(
                """UPDATE graph_nodes SET state='cancelled', updated_at=?
                   WHERE workflow_id=? AND node_id=? AND state IN ('ready','blocked')""",
                (timestamp, workflow_id, node_id),
            )
            unit_cursor = connection.execute(
                """UPDATE work_units SET state='cancelled'
                   WHERE workflow_id=? AND node_id=? AND state IN ('ready','blocked')""",
                (workflow_id, node_id),
            )
            if node_cursor.rowcount != 1 or unit_cursor.rowcount != 1:
                raise RuntimeError(f"node/work unit state divergence: {node_id}")
            successors = connection.execute(
                """SELECT successor_node_id FROM graph_edges
                   WHERE workflow_id=? AND predecessor_node_id=?""",
                (workflow_id, node_id),
            ).fetchall()
            for successor in successors:
                RunStore._evaluate_node(
                    connection, workflow_id, str(successor["successor_node_id"]), timestamp
                )
            return
        satisfied = all(
            (row["requirement"] == "required_success" and row["state"] == "succeeded")
            or (row["requirement"] == "required_completion" and row["state"] in terminal)
            for row in predecessors
        )
        state = "ready" if satisfied else "blocked"
        node_cursor = connection.execute(
            """UPDATE graph_nodes SET state=?, updated_at=?
               WHERE workflow_id=? AND node_id=? AND state IN ('ready','blocked')""",
            (state, timestamp, workflow_id, node_id),
        )
        unit_cursor = connection.execute(
            """UPDATE work_units SET state=?, available_at=CASE WHEN ?='ready' THEN ? ELSE available_at END
               WHERE workflow_id=? AND node_id=? AND state IN ('ready','blocked')""",
            (state, state, timestamp, workflow_id, node_id),
        )
        if node_cursor.rowcount != 1 or unit_cursor.rowcount != 1:
            raise RuntimeError(f"node/work unit state divergence: {node_id}")

    @classmethod
    def _materialize_work_unit(cls, row: sqlite3.Row) -> WorkUnit:
        try:
            capabilities_data = json.loads(str(row["capabilities_json"]))
            conflicts_data = json.loads(str(row["conflicts_json"]))
            payload_data = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("stored work unit contains malformed JSON") from exc
        if not isinstance(capabilities_data, list) or not capabilities_data or any(
            not isinstance(item, str) or not item.strip() for item in capabilities_data
        ):
            raise ValueError("stored work unit capabilities are invalid")
        if len(set(capabilities_data)) != len(capabilities_data):
            raise ValueError("stored work unit capabilities are not unique")
        if not isinstance(payload_data, dict):
            raise ValueError("stored work unit payload must be a JSON object")
        if not isinstance(conflicts_data, list):
            raise ValueError("stored work unit conflicts are invalid")
        conflicts: list[ConflictRequest] = []
        domains: set[str] = set()
        try:
            for item in conflicts_data:
                if not isinstance(item, dict) or set(item) != {"domain", "mode"}:
                    raise ValueError("stored work unit conflict is invalid")
                domain = cls._required_text(item["domain"], field="stored conflict domain")
                if domain in domains:
                    raise ValueError("stored work unit conflict domains are not unique")
                domains.add(domain)
                conflicts.append(ConflictRequest(domain, LeaseMode(item["mode"])))
            created_at = datetime.fromisoformat(str(row["created_at"]))
            cls._utc_timestamp(created_at, field="stored work unit timestamp")
            profile = row["profile"]
            if profile is not None:
                cls._required_text(profile, field="stored profile")
            return WorkUnit(
                unit_id=cls._required_text(row["unit_id"], field="stored work unit id"),
                workflow_id=cls._required_text(row["workflow_id"], field="stored workflow id"),
                node_id=cls._required_text(row["node_id"], field="stored node id"),
                workflow_kind=WorkflowKind(row["kind"]),
                node_type=NodeType(row["node_type"]),
                profile=str(profile) if profile is not None else None,
                capabilities=tuple(capabilities_data),
                conflicts=tuple(conflicts),
                payload=payload_data,
                priority=int(row["priority"]),
                created_at=created_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith("stored work unit"):
                raise
            raise ValueError("stored work unit is invalid") from exc

    def claim_next(
        self,
        profile: str,
        capabilities: set[str] | frozenset[str],
        runner_pid: int,
        runner_start: str,
        now: datetime,
    ) -> ClaimedWorkUnit | None:
        if runner_pid <= 0 or not runner_start:
            raise ValueError("runner PID and process-start identity are required")
        capability_set = set(capabilities)
        now_timestamp = self._utc_timestamp(now, field="claim timestamp")
        attempt_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """SELECT 1 FROM attempts WHERE profile=? AND state='claimed'
                   UNION ALL
                   SELECT 1 FROM runs WHERE profile=? AND state IN ('prepared','running')
                   LIMIT 1""",
                (profile, profile),
            ).fetchone()
            if active is not None:
                connection.commit()
                return None
            predecessor_divergence = connection.execute(
                """SELECT e.predecessor_node_id
                   FROM work_units successor_unit
                   JOIN graph_nodes successor_node
                     ON successor_node.workflow_id=successor_unit.workflow_id
                    AND successor_node.node_id=successor_unit.node_id
                   JOIN workflow_instances w ON w.workflow_id=successor_unit.workflow_id
                   JOIN graph_edges e
                     ON e.workflow_id=successor_unit.workflow_id
                    AND e.successor_node_id=successor_unit.node_id
                   LEFT JOIN graph_nodes predecessor_node
                     ON predecessor_node.workflow_id=e.workflow_id
                    AND predecessor_node.node_id=e.predecessor_node_id
                   LEFT JOIN work_units predecessor_unit
                     ON predecessor_unit.workflow_id=e.workflow_id
                    AND predecessor_unit.node_id=e.predecessor_node_id
                   WHERE successor_unit.state='ready' AND successor_node.state='ready'
                     AND w.state IN ('queued','active') AND successor_unit.available_at <= ?
                     AND (successor_unit.profile IS NULL OR successor_unit.profile=?)
                     AND (predecessor_node.node_id IS NULL
                          OR predecessor_unit.unit_id IS NULL
                          OR predecessor_node.state<>predecessor_unit.state)
                   LIMIT 1""",
                (now_timestamp, profile),
            ).fetchone()
            if predecessor_divergence is not None:
                raise RuntimeError(
                    f"predecessor state divergence: {predecessor_divergence['predecessor_node_id']}"
                )
            rows = connection.execute(
                """SELECT u.*, n.node_type, w.kind
                   FROM work_units u
                   JOIN graph_nodes n ON n.node_id=u.node_id
                   JOIN workflow_instances w ON w.workflow_id=u.workflow_id
                   WHERE u.state='ready' AND n.state='ready'
                     AND w.state IN ('queued','active') AND u.available_at <= ?
                     AND (u.profile IS NULL OR u.profile=?)
                     AND NOT EXISTS (
                         SELECT 1 FROM attempts active_attempt
                         WHERE active_attempt.unit_id=u.unit_id
                           AND active_attempt.state='claimed'
                     )
                     AND NOT EXISTS (
                         SELECT 1
                         FROM graph_edges e
                         JOIN graph_nodes predecessor_node
                           ON predecessor_node.workflow_id=e.workflow_id
                          AND predecessor_node.node_id=e.predecessor_node_id
                         JOIN work_units predecessor_unit
                           ON predecessor_unit.workflow_id=e.workflow_id
                          AND predecessor_unit.node_id=e.predecessor_node_id
                         WHERE e.workflow_id=u.workflow_id
                           AND e.successor_node_id=u.node_id
                           AND NOT (
                               predecessor_node.state=predecessor_unit.state
                               AND (
                                   (e.requirement='required_success'
                                    AND predecessor_node.state='succeeded')
                                   OR (e.requirement='required_completion'
                                       AND predecessor_node.state IN ('succeeded','failed','cancelled'))
                               )
                           )
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM runs r
                         WHERE r.repo=w.repo AND r.issue=w.issue
                           AND r.state IN ('prepared','running')
                     )
                   ORDER BY u.priority DESC, u.available_at, u.created_at, u.unit_id""",
                (now_timestamp, profile),
            ).fetchall()
            for row in rows:
                unit = self._materialize_work_unit(row)
                required = set(unit.capabilities)
                if not required.issubset(capability_set):
                    continue
                conflict_data: list[dict[str, object]] = [
                    {"domain": item.domain, "mode": item.mode.value} for item in unit.conflicts
                ]
                if not self._conflicts_available(connection, conflict_data):
                    continue
                attempt_number = int(row["current_attempt_number"]) + 1
                with self._authorized_transitions(connection):
                    cursor = connection.execute(
                        """UPDATE work_units SET state='claimed', current_attempt_number=?
                           WHERE unit_id=? AND state='ready'""",
                        (attempt_number, row["unit_id"]),
                    )
                    if cursor.rowcount != 1:
                        continue
                    node_cursor = connection.execute(
                        "UPDATE graph_nodes SET state='claimed', updated_at=? WHERE node_id=? AND state='ready'",
                        (now_timestamp, row["node_id"]),
                    )
                    if node_cursor.rowcount != 1:
                        raise RuntimeError(f"claim state divergence: {row['unit_id']}")
                if row["kind"] not in (WorkflowKind.SINGLE.value, WorkflowKind.GRAPH.value):
                    raise RuntimeError(f"claim workflow divergence: {row['workflow_id']}")
                workflow_cursor = connection.execute(
                    "UPDATE workflow_instances SET state='active', updated_at=? WHERE workflow_id=? AND state='queued'",
                    (now_timestamp, row["workflow_id"]),
                )
                workflow_state = connection.execute(
                    "SELECT state FROM workflow_instances WHERE workflow_id=?",
                    (row["workflow_id"],),
                ).fetchone()
                if workflow_state is None or workflow_state["state"] != "active":
                    raise RuntimeError(f"claim workflow divergence: {row['workflow_id']}")
                if workflow_cursor.rowcount not in (0, 1):
                    raise RuntimeError(f"claim workflow divergence: {row['workflow_id']}")
                connection.execute(
                    """INSERT INTO attempts(
                        attempt_id, unit_id, attempt_number, profile, claim_token, state,
                        runner_pid, runner_start, claimed_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?)""",
                    (attempt_id, row["unit_id"], attempt_number, profile, token,
                     runner_pid, runner_start, now_timestamp, now_timestamp),
                )
                conflicts = unit.conflicts
                for conflict in conflicts:
                    connection.execute(
                        "INSERT INTO conflict_leases(domain, attempt_id, mode) VALUES (?, ?, ?)",
                        (conflict.domain, attempt_id, conflict.mode.value),
                    )
                connection.commit()
                return ClaimedWorkUnit(unit, attempt_id, attempt_number, token)
            connection.commit()
            return None

    @staticmethod
    def _conflicts_available(
        connection: sqlite3.Connection, requests: Iterable[dict[str, object]]
    ) -> bool:
        for request in requests:
            domain = str(request["domain"])
            mode = LeaseMode(str(request["mode"]))
            if mode is LeaseMode.EXCLUSIVE:
                row = connection.execute(
                    "SELECT 1 FROM conflict_leases WHERE domain=? LIMIT 1", (domain,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT 1 FROM conflict_leases WHERE domain=? AND mode='exclusive' LIMIT 1",
                    (domain,),
                ).fetchone()
            if row is not None:
                return False
        return True

    @staticmethod
    def _current_claim(
        connection: sqlite3.Connection, attempt_id: str, claim_token: str | None = None
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT a.*, u.workflow_id, u.node_id, u.state AS unit_state,
                      u.current_attempt_number, n.state AS node_state,
                      w.state AS workflow_state,
                      (SELECT COUNT(*) FROM attempts sibling
                       WHERE sibling.unit_id=u.unit_id AND sibling.state='claimed')
                          AS claimed_attempt_count
               FROM attempts a
               JOIN work_units u ON u.unit_id=a.unit_id
               JOIN graph_nodes n ON n.node_id=u.node_id AND n.workflow_id=u.workflow_id
               JOIN workflow_instances w ON w.workflow_id=u.workflow_id
               WHERE a.attempt_id=?""",
            (attempt_id,),
        ).fetchone()
        if row is None or (claim_token is not None and row["claim_token"] != claim_token):
            raise KeyError(f"current claim not found: {attempt_id}")
        if row["state"] != "claimed":
            raise KeyError(f"current claim not found: {attempt_id}")
        if (
            row["unit_state"] != "claimed"
            or row["node_state"] != "claimed"
            or row["workflow_state"] != "active"
            or int(row["attempt_number"]) != int(row["current_attempt_number"])
            or int(row["claimed_attempt_count"]) != 1
        ):
            raise RuntimeError(f"attempt state divergence: {attempt_id}")
        return row

    def heartbeat_attempt(self, attempt_id: str, claim_token: str, at: datetime) -> None:
        timestamp = self._utc_timestamp(at, field="heartbeat timestamp")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._current_claim(connection, attempt_id, claim_token)
            cursor = connection.execute(
                """UPDATE attempts SET heartbeat_at=?
                   WHERE attempt_id=? AND claim_token=? AND state='claimed'""",
                (timestamp, attempt_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"current claim not found: {attempt_id}")
            connection.commit()

    def reconcile_stale_attempts(
        self,
        now: datetime,
        is_alive: Callable[[int, str], bool | None],
        *,
        max_attempts: int,
    ) -> list[str]:
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts <= 0:
            raise ValueError("max_attempts must be a positive integer")
        timestamp = self._utc_timestamp(now, field="reconciliation timestamp")
        proven_dead: list[tuple[str, str, int, str]] = []
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT attempt_id, claim_token, runner_pid, runner_start, heartbeat_at
                   FROM attempts WHERE state='claimed'"""
            ).fetchall()
        for row in rows:
            try:
                heartbeat = datetime.fromisoformat(str(row["heartbeat_at"]))
                if heartbeat.tzinfo is None or now.astimezone(UTC) - heartbeat.astimezone(UTC) < _STALE_AFTER:
                    continue
                alive = is_alive(int(row["runner_pid"]), str(row["runner_start"]))
            except Exception:
                continue
            if alive is False:
                proven_dead.append(
                    (str(row["attempt_id"]), str(row["claim_token"]),
                     int(row["runner_pid"]), str(row["runner_start"]))
                )

        event_ids = {attempt_id: str(uuid.uuid4()) for attempt_id, _, _, _ in proven_dead}
        reconciled: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for attempt_id, claim_token, runner_pid, runner_start in proven_dead:
                try:
                    current = self._current_claim(connection, attempt_id, claim_token)
                except KeyError:
                    continue
                row = connection.execute(
                    """SELECT a.attempt_number, a.heartbeat_at, a.unit_id,
                              u.workflow_id, u.node_id, u.state AS unit_state,
                              n.state AS node_state, w.state AS workflow_state
                       FROM attempts a
                       JOIN work_units u ON u.unit_id=a.unit_id
                       JOIN graph_nodes n ON n.node_id=u.node_id AND n.workflow_id=u.workflow_id
                       JOIN workflow_instances w ON w.workflow_id=u.workflow_id
                       WHERE a.attempt_id=? AND a.state='claimed'
                         AND a.claim_token=? AND a.runner_pid=? AND a.runner_start=?
                         AND a.attempt_number=u.current_attempt_number""",
                    (attempt_id, claim_token, runner_pid, runner_start),
                ).fetchone()
                if row is None:
                    continue
                heartbeat = datetime.fromisoformat(str(row["heartbeat_at"]))
                if now.astimezone(UTC) - heartbeat.astimezone(UTC) < _STALE_AFTER:
                    continue
                if current["runner_pid"] != runner_pid or current["runner_start"] != runner_start:
                    continue
                exhausted = int(row["attempt_number"]) >= max_attempts
                result = self._bounded_json(
                    {"schema_version": 1, "summary": "stale runner identity proven dead"},
                    field="stale attempt result",
                )
                with self._authorized_transitions(connection):
                    connection.execute("DELETE FROM conflict_leases WHERE attempt_id=?", (attempt_id,))
                    cursor = connection.execute(
                        """UPDATE attempts
                           SET state='failed', completed_at=?, heartbeat_at=?, result_json=?
                           WHERE attempt_id=? AND state='claimed'
                             AND claim_token=? AND runner_pid=? AND runner_start=?""",
                        (timestamp, timestamp, result, attempt_id, claim_token,
                         runner_pid, runner_start),
                    )
                    if cursor.rowcount != 1:
                        continue
                    next_state = "failed" if exhausted else "ready"
                    unit_cursor = connection.execute(
                        """UPDATE work_units SET state=?, available_at=?
                           WHERE unit_id=? AND state='claimed'""",
                        (next_state, timestamp, row["unit_id"]),
                    )
                    node_cursor = connection.execute(
                        """UPDATE graph_nodes SET state=?, updated_at=?
                           WHERE node_id=? AND state='claimed'""",
                        (next_state, timestamp, row["node_id"]),
                    )
                    if unit_cursor.rowcount != 1 or node_cursor.rowcount != 1:
                        raise RuntimeError(f"attempt state divergence: {attempt_id}")
                payload = self._bounded_json(
                    {"attempt_number": int(row["attempt_number"]), "outcome": next_state},
                    field="stale attempt event",
                )
                connection.execute(
                    """INSERT INTO events(
                        event_id, idempotency_key, workflow_id, node_id, unit_id,
                        attempt_id, event_type, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'stale_attempt_reconciled', ?, ?)""",
                    (event_ids[attempt_id], f"stale-attempt:{attempt_id}", row["workflow_id"],
                     row["node_id"], row["unit_id"], attempt_id, payload, timestamp),
                )
                if exhausted:
                    with self._authorized_transitions(connection):
                        self._unlock_successors(
                            connection, str(row["workflow_id"]), str(row["node_id"]), now
                        )
                        self._terminalize_workflow_if_complete(
                            connection, str(row["workflow_id"]), timestamp
                        )
                reconciled.append(attempt_id)
            connection.commit()
        return reconciled

    def register_artifact(
        self,
        artifact_id: str,
        workflow_id: str,
        node_id: str,
        attempt_id: str,
        claim_token: str,
        artifact_type: str,
        schema_version: int,
        source_revision: str,
        sha256: str,
        summary: dict[str, object],
        location: str,
        at: datetime,
    ) -> bool:
        for value, field in (
            (artifact_id, "artifact id"),
            (workflow_id, "workflow id"),
            (node_id, "node id"),
            (attempt_id, "attempt id"),
            (claim_token, "claim token"),
            (artifact_type, "artifact type"),
            (source_revision, "source revision"),
        ):
            self._required_text(value, field=field)
        if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version <= 0:
            raise ValueError("artifact schema version must be a positive integer")
        if not isinstance(sha256, str) or len(sha256) != 64 or any(
            character not in "0123456789abcdef" for character in sha256
        ):
            raise ValueError("artifact SHA-256 must be 64 lowercase hexadecimal characters")
        if not isinstance(summary, dict):
            raise ValueError("artifact summary must be a JSON object")
        summary_json = self._bounded_json(summary, field="artifact summary")
        if not isinstance(location, str) or "\\" in location:
            raise ValueError("artifact location must be a safe relative path")
        location_path = PurePosixPath(location)
        if (
            not location
            or location_path.is_absolute()
            or any(part in ("", ".", "..") for part in location_path.parts)
        ):
            raise ValueError("artifact location must be a safe relative path")
        if location_path.name != sha256:
            raise ValueError("artifact location must be content-addressed by SHA-256")
        timestamp = self._utc_timestamp(at, field="artifact timestamp")
        expected = (
            workflow_id, node_id, attempt_id, artifact_type, schema_version,
            source_revision, sha256, summary_json, location, timestamp,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = self._current_claim(connection, attempt_id, claim_token)
            if (attempt["workflow_id"], attempt["node_id"]) != (workflow_id, node_id):
                raise ValueError("artifact provenance mismatch")
            prior = connection.execute(
                """SELECT workflow_id, node_id, attempt_id, artifact_type, schema_version,
                          source_revision, sha256, summary_json, location, created_at
                   FROM artifacts WHERE artifact_id=?""",
                (artifact_id,),
            ).fetchone()
            if prior is not None:
                if tuple(prior) != expected:
                    raise ValueError(f"artifact id collision: {artifact_id}")
                connection.commit()
                return False
            connection.execute(
                """INSERT INTO artifacts(
                    artifact_id, workflow_id, node_id, attempt_id, artifact_type,
                    schema_version, source_revision, sha256, summary_json, location, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (artifact_id, *expected),
            )
            connection.commit()
            return True

    @staticmethod
    def _validate_agent_result(result: object) -> None:
        if not isinstance(result, dict):
            raise ValueError("attempt result must be a report object")
        allowed = {"schema_version", "summary", "findings", "artifact_ids"}
        unknown = set(result) - allowed
        if unknown:
            raise ValueError(f"attempt result contains unknown field: {sorted(unknown)[0]}")
        if set(result) < {"schema_version", "summary"}:
            raise ValueError("attempt result requires schema_version and summary")
        version = result["schema_version"]
        if not isinstance(version, int) or isinstance(version, bool) or version != 1:
            raise ValueError("attempt result schema_version must be integer 1")
        summary = result["summary"]
        if not isinstance(summary, str) or len(summary.encode("utf-8")) > 16_384:
            raise ValueError("attempt result summary must be text of at most 16384 bytes")
        findings = result.get("findings", [])
        if not isinstance(findings, list) or len(findings) > 20:
            raise ValueError("attempt result findings must be a list of at most 20 items")
        finding_fields = {"summary", "path", "line", "severity", "details", "artifact_id"}
        for finding in findings:
            if not isinstance(finding, dict) or "summary" not in finding:
                raise ValueError("each finding must be an object with a summary")
            finding_unknown = set(finding) - finding_fields
            if finding_unknown:
                raise ValueError(f"finding contains unknown field: {sorted(finding_unknown)[0]}")
            finding_summary = finding["summary"]
            if not isinstance(finding_summary, str) or not finding_summary.strip() or len(
                finding_summary.encode("utf-8")
            ) > 4096:
                raise ValueError("finding summary must be nonempty text of at most 4096 bytes")
            for field, limit in (("path", 4096), ("details", 16_384), ("artifact_id", 1024)):
                value = finding.get(field)
                if value is not None and (
                    not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit
                ):
                    raise ValueError(f"finding {field} must be bounded nonempty text")
            line = finding.get("line")
            if line is not None and (
                not isinstance(line, int) or isinstance(line, bool) or line <= 0
            ):
                raise ValueError("finding line must be a positive integer")
            severity = finding.get("severity")
            if severity is not None and severity not in {"info", "low", "medium", "high", "critical"}:
                raise ValueError("finding severity is invalid")
        artifact_ids = result.get("artifact_ids", [])
        if not isinstance(artifact_ids, list) or len(artifact_ids) > 20 or any(
            not isinstance(item, str) or not item.strip() or len(item.encode("utf-8")) > 1024
            for item in artifact_ids
        ):
            raise ValueError("attempt result artifact_ids must contain at most 20 bounded strings")
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("attempt result artifact_ids must be unique")

    def complete_attempt(
        self,
        attempt_id: str,
        claim_token: str,
        outcome: str,
        result: dict[str, object],
        at: datetime,
        idempotency_key: str,
    ) -> bool:
        if outcome not in ("succeeded", "failed"):
            raise ValueError("attempt outcome must be succeeded or failed")
        self._validate_agent_result(result)
        result_json = self._bounded_json(result, field="attempt result")
        event_payload = self._bounded_json(
            {"outcome": outcome, "result": result}, field="completion event"
        )
        timestamp = self._utc_timestamp(at, field="completion timestamp")
        event_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                """SELECT a.state, a.claim_token, a.unit_id, u.workflow_id, u.node_id,
                          u.state AS unit_state, n.state AS node_state,
                          w.state AS workflow_state
                   FROM attempts a
                   JOIN work_units u ON u.unit_id=a.unit_id
                   JOIN graph_nodes n ON n.node_id=u.node_id AND n.workflow_id=u.workflow_id
                   JOIN workflow_instances w ON w.workflow_id=u.workflow_id
                   WHERE a.attempt_id=?""",
                (attempt_id,),
            ).fetchone()
            if attempt is None or attempt["claim_token"] != claim_token:
                raise KeyError(f"current claim not found: {attempt_id}")
            if attempt["state"] != "claimed":
                event = connection.execute(
                    """SELECT event_type, payload_json, attempt_id FROM events
                       WHERE idempotency_key=?""",
                    (idempotency_key,),
                ).fetchone()
                if event is not None and (
                    event["event_type"], event["payload_json"], event["attempt_id"]
                ) == ("attempt_completed", event_payload, attempt_id):
                    connection.commit()
                    return False
                raise KeyError(f"current claim not found: {attempt_id}")
            attempt = self._current_claim(connection, attempt_id, claim_token)
            collision = connection.execute(
                "SELECT 1 FROM events WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if collision is not None:
                raise ValueError(f"event idempotency key collision: {idempotency_key}")
            with self._authorized_transitions(connection):
                connection.execute("DELETE FROM conflict_leases WHERE attempt_id=?", (attempt_id,))
                cursor = connection.execute(
                    """UPDATE attempts
                       SET state=?, completed_at=?, heartbeat_at=?, result_json=?
                       WHERE attempt_id=? AND claim_token=? AND state='claimed'""",
                    (outcome, timestamp, timestamp, result_json, attempt_id, claim_token),
                )
                if cursor.rowcount != 1:
                    raise KeyError(f"current claim not found: {attempt_id}")
                unit_cursor = connection.execute(
                    "UPDATE work_units SET state=? WHERE unit_id=? AND state='claimed'",
                    (outcome, attempt["unit_id"]),
                )
                node_cursor = connection.execute(
                    "UPDATE graph_nodes SET state=?, updated_at=? WHERE node_id=? AND state='claimed'",
                    (outcome, timestamp, attempt["node_id"]),
                )
                if unit_cursor.rowcount != 1 or node_cursor.rowcount != 1:
                    raise RuntimeError(f"attempt state divergence: {attempt_id}")
            connection.execute(
                """INSERT INTO events(
                    event_id, idempotency_key, workflow_id, node_id, unit_id,
                    attempt_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'attempt_completed', ?, ?)""",
                (event_id, idempotency_key, attempt["workflow_id"], attempt["node_id"],
                 attempt["unit_id"], attempt_id, event_payload, timestamp),
            )
            with self._authorized_transitions(connection):
                self._unlock_successors(
                    connection, str(attempt["workflow_id"]), str(attempt["node_id"]), at
                )
                self._terminalize_workflow_if_complete(
                    connection, str(attempt["workflow_id"]), timestamp
                )
            connection.commit()
            return True

    @staticmethod
    def _terminalize_workflow_if_complete(
        connection: sqlite3.Connection, workflow_id: str, timestamp: str
    ) -> None:
        remaining = int(connection.execute(
            """SELECT COUNT(*) FROM graph_nodes
               WHERE workflow_id=? AND state NOT IN ('succeeded','failed','cancelled')""",
            (workflow_id,),
        ).fetchone()[0])
        if remaining != 0:
            return
        failures = int(connection.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE workflow_id=? AND state<>'succeeded'",
            (workflow_id,),
        ).fetchone()[0])
        cursor = connection.execute(
            """UPDATE workflow_instances SET state=?, updated_at=?
               WHERE workflow_id=? AND state='active'""",
            ("failed" if failures else "succeeded", timestamp, workflow_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"workflow state divergence: {workflow_id}")

    @staticmethod
    def _unlock_successors(
        connection: sqlite3.Connection, workflow_id: str, completed_node_id: str, at: datetime
    ) -> None:
        successors = connection.execute(
            """SELECT successor_node_id FROM graph_edges
               WHERE workflow_id=? AND predecessor_node_id=?""",
            (workflow_id, completed_node_id),
        ).fetchall()
        timestamp = RunStore._utc_timestamp(at, field="completion timestamp")
        for successor in successors:
            RunStore._evaluate_node(
                connection, workflow_id, str(successor["successor_node_id"]), timestamp
            )

    def reserve(self, assignment: Assignment, run_id: str, at: datetime) -> bool:
        candidate = assignment.candidate
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                owned = connection.execute(
                    """SELECT 1
                       FROM attempts a
                       JOIN work_units u ON u.unit_id=a.unit_id
                       JOIN workflow_instances w ON w.workflow_id=u.workflow_id
                       WHERE a.state='claimed'
                         AND (a.profile=? OR (w.repo=? AND w.issue=?))
                       LIMIT 1""",
                    (assignment.worker.profile, candidate.repo, candidate.number),
                ).fetchone()
                if owned is not None:
                    connection.commit()
                    return False
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, repo, issue, profile, role, state,
                        created_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?, 'prepared', ?, ?)
                    """,
                    (
                        run_id,
                        candidate.repo,
                        candidate.number,
                        assignment.worker.profile,
                        candidate.role.value,
                        at.isoformat(),
                        at.isoformat(),
                    ),
                )
                connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_running(
        self,
        run_id: str,
        *,
        pid: int,
        process_start: str,
        at: datetime,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET state='running', pid=?, process_start=?, heartbeat_at=?
                WHERE run_id=? AND state='prepared'
                """,
                (pid, process_start, at.isoformat(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"prepared run not found: {run_id}")

    def attach_workspace(self, run_id: str, context: dict[str, object]) -> None:
        encoded = json.dumps(context, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET workspace_json=? WHERE run_id=? AND state IN ('prepared','running')",
                (encoded, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"active run not found: {run_id}")

    def get_run(self, run_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def stale_run_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM runs WHERE state='stale' ORDER BY created_at, run_id"
            ).fetchall()
        return [str(row["run_id"]) for row in rows]

    def heartbeat(self, run_id: str, at: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET heartbeat_at=? WHERE run_id=? AND state='running'",
                (at.isoformat(), run_id),
            )

    def finish(self, run_id: str, outcome: str, result_path: Path | None = None) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE runs SET state='finished', outcome=?, result_path=?
                WHERE run_id=? AND state IN ('prepared', 'running', 'stale')
                """,
                (outcome, str(result_path) if result_path else None, run_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT state, outcome, result_path FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                expected_path = str(result_path) if result_path else None
                if row is None or (
                    str(row["state"]), row["outcome"], row["result_path"]
                ) != ("finished", outcome, expected_path):
                    raise KeyError(f"active run not found: {run_id}")

    def is_finished(
        self, run_id: str, outcome: str, result_path: Path | None = None
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state, outcome, result_path FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return row is not None and (
            str(row["state"]), row["outcome"], row["result_path"]
        ) == ("finished", outcome, str(result_path) if result_path else None)

    def active_profiles(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT profile FROM runs
                   WHERE state IN ('prepared', 'running')
                   UNION
                   SELECT a.profile
                   FROM attempts a
                   JOIN work_units u ON u.unit_id=a.unit_id
                   JOIN graph_nodes n
                     ON n.node_id=u.node_id AND n.workflow_id=u.workflow_id
                   JOIN workflow_instances w ON w.workflow_id=u.workflow_id
                   WHERE a.state='claimed' AND u.state='claimed'
                     AND n.state='claimed' AND w.state='active'
                     AND a.attempt_number=u.current_attempt_number"""
            ).fetchall()
        return {str(row["profile"]) for row in rows}

    def active_issues(self) -> set[tuple[str, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT repo, issue FROM runs
                   WHERE state IN ('prepared', 'running')
                   UNION
                   SELECT w.repo, w.issue
                   FROM attempts a
                   JOIN work_units u ON u.unit_id=a.unit_id
                   JOIN graph_nodes n
                     ON n.node_id=u.node_id AND n.workflow_id=u.workflow_id
                   JOIN workflow_instances w ON w.workflow_id=u.workflow_id
                   WHERE a.state='claimed' AND u.state='claimed'
                     AND n.state='claimed' AND w.state='active'
                     AND a.attempt_number=u.current_attempt_number"""
            ).fetchall()
        return {(str(row["repo"]), int(row["issue"])) for row in rows}

    def reclaim_stale(
        self,
        now: datetime,
        is_alive: Callable[[int, str], bool],
    ) -> list[str]:
        reclaimed: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT run_id, heartbeat_at, pid, process_start
                FROM runs WHERE state IN ('prepared', 'running')
                """
            ).fetchall()
            for row in rows:
                heartbeat = datetime.fromisoformat(str(row["heartbeat_at"]))
                if now - heartbeat < _STALE_AFTER:
                    continue
                pid = row["pid"]
                start = row["process_start"]
                if pid is not None and start is not None and is_alive(int(pid), str(start)):
                    continue
                run_id = str(row["run_id"])
                connection.execute(
                    "UPDATE runs SET state='stale', outcome='stale' WHERE run_id=?",
                    (run_id,),
                )
                reclaimed.append(run_id)
            connection.commit()
        return reclaimed
