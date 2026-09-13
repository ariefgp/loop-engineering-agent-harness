from __future__ import annotations

import os
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .scheduler import Assignment


_ACTIVE = ("prepared", "running")
_STALE_AFTER = timedelta(minutes=45)


class RunStore:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
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

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                timeout=10,
                isolation_level=None,
                uri=True,
            )
        else:
            connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        if self.read_only:
            connection.execute("PRAGMA query_only=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=DELETE;
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
                    result_path TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_issue
                    ON runs(repo, issue) WHERE state IN ('prepared', 'running');
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_profile
                    ON runs(profile) WHERE state IN ('prepared', 'running');
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', '1');
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "workspace_json" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN workspace_json TEXT")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def reserve(self, assignment: Assignment, run_id: str, at: datetime) -> bool:
        candidate = assignment.candidate
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
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
                "SELECT profile FROM runs WHERE state IN ('prepared', 'running')"
            ).fetchall()
        return {str(row["profile"]) for row in rows}

    def active_issues(self) -> set[tuple[str, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT repo, issue FROM runs WHERE state IN ('prepared', 'running')"
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
