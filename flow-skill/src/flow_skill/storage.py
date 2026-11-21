"""SQLite persistence for workflow runs, tasks, and events."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .errors import RunNotFoundError, TaskNotFoundError
from .models import RunStatus, TaskStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode_json(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


RUN_MUTABLE_FIELDS = {"status", "updated_at"}
TASK_MUTABLE_FIELDS = {
    "status",
    "retry_count",
    "inputs_json",
    "outputs_json",
    "started_at",
    "finished_at",
    "error_json",
}


class Storage:
    """Small repository layer with explicit SQLite transactions."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=5,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    flow_name TEXT NOT NULL,
                    flow_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    inputs_json TEXT NOT NULL,
                    workspace_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_tasks (
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    task_order INTEGER NOT NULL,
                    task_type TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL,
                    mode TEXT,
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 0,
                    stale_after_seconds INTEGER,
                    inputs_json TEXT NOT NULL,
                    outputs_json TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    error_json TEXT,
                    PRIMARY KEY (run_id, task_id),
                    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_tasks_run_status
                    ON workflow_tasks(run_id, status, task_order);

                CREATE TABLE IF NOT EXISTS workflow_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    task_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_events_run_event
                    ON workflow_events(run_id, event_id);
                """
            )
        finally:
            connection.close()

    def create_run(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        flow_name: str,
        flow_version: str,
        inputs: Mapping[str, Any],
        workspace_path: Path,
        tasks: Sequence[Mapping[str, Any]],
    ) -> None:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO workflow_runs (
                run_id, flow_name, flow_version, status, inputs_json,
                workspace_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                flow_name,
                flow_version,
                RunStatus.RUNNING,
                encode_json(dict(inputs)),
                str(workspace_path),
                now,
                now,
            ),
        )
        for order, task in enumerate(tasks):
            connection.execute(
                """
                INSERT INTO workflow_tasks (
                    run_id, task_id, task_order, task_type, instruction,
                    dependencies_json, mode, metadata_json, status,
                    retry_count, max_retries, stale_after_seconds, inputs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    run_id,
                    task["id"],
                    order,
                    task["task_type"],
                    task["instruction"],
                    encode_json(list(task.get("after", ()))),
                    task.get("mode"),
                    encode_json(task.get("metadata", {})),
                    TaskStatus.PENDING,
                    task.get("max_retries", 0),
                    task.get("stale_after"),
                    encode_json(task.get("inputs", {})),
                ),
            )
        self.add_event(
            connection,
            run_id=run_id,
            event_type="run_created",
            payload={"flow_name": flow_name, "flow_version": flow_version},
        )

    def get_run(self, connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"Run '{run_id}' does not exist")
        return self._run_from_row(row)

    def get_task(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM workflow_tasks WHERE run_id = ? AND task_id = ?",
            (run_id, task_id),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task '{task_id}' does not exist in run '{run_id}'")
        return self._task_from_row(row)

    def list_tasks(self, connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM workflow_tasks WHERE run_id = ? ORDER BY task_order",
            (run_id,),
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def update_run(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        **fields: Any,
    ) -> None:
        unknown = set(fields) - RUN_MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported run fields: {', '.join(sorted(unknown))}")
        fields.setdefault("updated_at", utc_now())
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values()) + [run_id]
        connection.execute(
            f"UPDATE workflow_runs SET {assignments} WHERE run_id = ?",  # noqa: S608
            values,
        )

    def update_task(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        task_id: str,
        **fields: Any,
    ) -> None:
        unknown = set(fields) - TASK_MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported task fields: {', '.join(sorted(unknown))}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values()) + [run_id, task_id]
        connection.execute(
            f"UPDATE workflow_tasks SET {assignments} WHERE run_id = ? AND task_id = ?",  # noqa: S608
            values,
        )

    def add_event(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        task_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO workflow_events (
                run_id, task_id, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, task_id, event_type, encode_json(dict(payload or {})), utc_now()),
        )

    def read_status(self, run_id: str) -> dict[str, Any]:
        with self.transaction() as connection:
            run = self.get_run(connection, run_id)
            tasks = self.list_tasks(connection, run_id)
        return {"run": run, "tasks": tasks}

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        with self.transaction() as connection:
            self.get_run(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM workflow_events WHERE run_id = ? ORDER BY event_id",
                (run_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "event_type": row["event_type"],
                "payload": decode_json(row["payload_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "flow_name": row["flow_name"],
            "flow_version": row["flow_version"],
            "status": row["status"],
            "inputs": decode_json(row["inputs_json"], {}),
            "workspace_path": row["workspace_path"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "task_order": row["task_order"],
            "task_type": row["task_type"],
            "instruction": row["instruction"],
            "dependencies": decode_json(row["dependencies_json"], []),
            "mode": row["mode"],
            "metadata": decode_json(row["metadata_json"], {}),
            "status": row["status"],
            "retry_count": row["retry_count"],
            "max_retries": row["max_retries"],
            "stale_after_seconds": row["stale_after_seconds"],
            "inputs": decode_json(row["inputs_json"], {}),
            "outputs": decode_json(row["outputs_json"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": decode_json(row["error_json"]),
        }
