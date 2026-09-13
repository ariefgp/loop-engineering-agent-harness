from __future__ import annotations

import os
import sqlite3
import tempfile
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
        self._snapshot: tempfile.TemporaryDirectory[str] | None = None
        if read_only:
            if not path.is_file():
                raise FileNotFoundError(path)
            self._snapshot = tempfile.TemporaryDirectory(prefix="loop-harness-shadow-")
            snapshot_path = Path(self._snapshot.name) / path.name
            source = sqlite3.connect(
                f"file:{path.resolve()}?mode=ro",
                timeout=10,
                uri=True,
            )
            destination = sqlite3.connect(snapshot_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            self.path = snapshot_path
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        self._initialize()

    def close(self) -> None:
        if self._snapshot is not None:
            self._snapshot.cleanup()
            self._snapshot = None

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            connection = sqlite3.connect(
                f"file:{self.path.resolve()}?mode=ro",
                timeout=10,
                isolation_level=None,
                uri=True,
            )
        else:
            connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
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
                WHERE run_id=? AND state IN ('prepared', 'running')
                """,
                (outcome, str(result_path) if result_path else None, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"active run not found: {run_id}")

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
