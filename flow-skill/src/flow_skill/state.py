"""Validated state transitions for runs and tasks."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import InvalidStateError
from .models import RunStatus, TaskStatus
from .storage import Storage, utc_now


TASK_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.PENDING: {
        TaskStatus.RUNNING,
        TaskStatus.WAITING_INPUT,
        TaskStatus.SKIPPED,
        TaskStatus.FAILED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.COMPLETED,
        TaskStatus.PENDING,
        TaskStatus.WAITING_RETRY_CONFIRMATION,
        TaskStatus.FAILED,
    },
    TaskStatus.WAITING_INPUT: {TaskStatus.COMPLETED, TaskStatus.FAILED},
    TaskStatus.WAITING_RETRY_CONFIRMATION: {TaskStatus.PENDING, TaskStatus.FAILED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.SKIPPED: set(),
}

RUN_TRANSITIONS: dict[str, set[str]] = {
    RunStatus.RUNNING: {RunStatus.WAITING, RunStatus.COMPLETED, RunStatus.FAILED},
    RunStatus.WAITING: {RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED},
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
}


class StateManager:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def transition_task(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        task_id: str,
        current_status: str,
        target_status: str,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        if target_status not in TASK_TRANSITIONS.get(current_status, set()):
            raise InvalidStateError(
                f"Task '{task_id}' cannot transition from '{current_status}' to '{target_status}'"
            )
        fields["status"] = target_status
        if target_status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED}:
            fields.setdefault("finished_at", utc_now())
        self.storage.update_task(connection, run_id, task_id, **fields)
        self.storage.add_event(
            connection,
            run_id=run_id,
            task_id=task_id,
            event_type=event_type,
            payload={
                "from": current_status,
                "to": target_status,
                **(event_payload or {}),
            },
        )

    def transition_run(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        current_status: str,
        target_status: str,
        event_type: str = "run_status_changed",
    ) -> None:
        if current_status == target_status:
            return
        if target_status not in RUN_TRANSITIONS.get(current_status, set()):
            raise InvalidStateError(
                f"Run '{run_id}' cannot transition from '{current_status}' to '{target_status}'"
            )
        self.storage.update_run(connection, run_id, status=target_status)
        self.storage.add_event(
            connection,
            run_id=run_id,
            event_type=event_type,
            payload={"from": current_status, "to": target_status},
        )

    @staticmethod
    def is_stale(task: dict[str, Any], *, now: datetime | None = None) -> bool:
        threshold = task.get("stale_after_seconds")
        started_at = task.get("started_at")
        if threshold is None or started_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        started = datetime.fromisoformat(started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return current >= started + timedelta(seconds=int(threshold))

