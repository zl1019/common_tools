"""Task scheduling, branching, stale recovery, and result submission."""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import (
    FlowDefinitionError,
    FlowVersionMismatchError,
    InvalidStateError,
    ValidationError,
)
from .models import (
    FlowResponse,
    ResponseStatus,
    ResultType,
    RunStatus,
    SubmissionResponse,
    SubmissionStatus,
    TaskStatus,
    TaskType,
    ValidationResult,
)
from .state import StateManager
from .storage import Storage, encode_json, utc_now
from .workflow import Flow, TaskContext, TaskDefinition

SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
TERMINAL_TASK_STATUSES = {TaskStatus.COMPLETED, TaskStatus.SKIPPED}


class Scheduler:
    def __init__(self, flow: Flow, *, data_dir: str | Path = ".flow-data") -> None:
        flow.validate()
        if not SAFE_ID.fullmatch(flow.name):
            raise FlowDefinitionError(
                "Flow name may contain only letters, numbers, dot, underscore, and hyphen"
            )
        self.flow = flow
        self.data_dir = Path(data_dir).resolve()
        self.flow_data_dir = self.data_dir / flow.name
        self.storage = Storage(self.flow_data_dir / "workflow.db")
        self.state = StateManager(self.storage)

    def create_run(self, *, inputs: Mapping[str, Any], run_id: str | None = None) -> str:
        selected_run_id = run_id or f"run_{uuid.uuid4().hex}"
        if not SAFE_ID.fullmatch(selected_run_id):
            raise ValidationError(
                "run_id may contain only letters, numbers, dot, underscore, and hyphen"
            )
        workspace = (self.flow_data_dir / "runs" / selected_run_id).resolve()
        expected_parent = (self.flow_data_dir / "runs").resolve()
        if workspace.parent != expected_parent:
            raise ValidationError("Resolved workspace is outside the flow run directory")
        for folder in ("inputs", "working", "outputs"):
            (workspace / folder).mkdir(parents=True, exist_ok=True)

        tasks = [
            {
                "id": task.id,
                "task_type": task.task_type,
                "instruction": task.instruction,
                "after": task.after,
                "mode": task.mode,
                "metadata": task.metadata,
                "max_retries": task.max_retries,
                "stale_after": task.stale_after,
            }
            for task in self.flow.tasks
        ]
        try:
            with self.storage.transaction(immediate=True) as connection:
                self.storage.create_run(
                    connection,
                    run_id=selected_run_id,
                    flow_name=self.flow.name,
                    flow_version=self.flow.version,
                    inputs=inputs,
                    workspace_path=workspace,
                    tasks=tasks,
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"Run '{selected_run_id}' already exists") from exc
        return selected_run_id

    def get_next_task(self, run_id: str) -> FlowResponse:
        with self.storage.transaction(immediate=True) as connection:
            run = self.storage.get_run(connection, run_id)
            self._validate_run_identity(run)
            if run["status"] == RunStatus.COMPLETED:
                return FlowResponse(status=ResponseStatus.COMPLETED, run_id=run_id)
            if run["status"] == RunStatus.FAILED:
                return FlowResponse(status=ResponseStatus.FAILED, run_id=run_id)

            tasks = self.storage.list_tasks(connection, run_id)
            active_response = self._active_task_response(connection, run, tasks)
            if active_response is not None:
                return active_response

            while True:
                tasks = self.storage.list_tasks(connection, run_id)
                task_map = {task["task_id"]: task for task in tasks}
                context = self._context(run, tasks)
                progressed = False

                for task in tasks:
                    if task["status"] != TaskStatus.PENDING:
                        continue
                    if not self._dependencies_satisfied(task, task_map):
                        continue

                    definition = self.flow.task(task["task_id"])
                    if not self._condition_matches(definition, context):
                        self.state.transition_task(
                            connection,
                            run_id=run_id,
                            task_id=task["task_id"],
                            current_status=TaskStatus.PENDING,
                            target_status=TaskStatus.SKIPPED,
                            event_type="task_skipped",
                            event_payload={"reason": "condition_false"},
                        )
                        progressed = True
                        break

                    if definition.task_type is TaskType.HUMAN:
                        self.state.transition_task(
                            connection,
                            run_id=run_id,
                            task_id=task["task_id"],
                            current_status=TaskStatus.PENDING,
                            target_status=TaskStatus.WAITING_INPUT,
                            event_type="human_input_requested",
                        )
                        self._set_run_status(connection, run, RunStatus.WAITING)
                        return self._human_response(run, task, definition)

                    self.state.transition_task(
                        connection,
                        run_id=run_id,
                        task_id=task["task_id"],
                        current_status=TaskStatus.PENDING,
                        target_status=TaskStatus.RUNNING,
                        event_type="task_started",
                        started_at=utc_now(),
                        finished_at=None,
                        error_json=None,
                    )
                    self._set_run_status(connection, run, RunStatus.RUNNING)
                    refreshed = self.storage.get_task(connection, run_id, task["task_id"])
                    return FlowResponse(
                        status=ResponseStatus.TASK_READY,
                        run_id=run_id,
                        task=self._task_payload(run, refreshed, definition, tasks),
                    )

                if progressed:
                    continue
                break

            tasks = self.storage.list_tasks(connection, run_id)
            if all(task["status"] in TERMINAL_TASK_STATUSES for task in tasks):
                gate = self._completion_gate(run, tasks)
                if gate.accepted:
                    self._set_run_status(connection, run, RunStatus.COMPLETED)
                    return FlowResponse(status=ResponseStatus.COMPLETED, run_id=run_id)
                self.storage.add_event(
                    connection,
                    run_id=run_id,
                    event_type="run_completion_gate_rejected",
                    payload={"errors": gate.errors},
                )
                self._set_run_status(connection, run, RunStatus.FAILED)
                return FlowResponse(
                    status=ResponseStatus.FAILED,
                    run_id=run_id,
                    reason="completion_gate_rejected",
                    task={"errors": gate.errors},
                )

            self._set_run_status(connection, run, RunStatus.WAITING)
            return FlowResponse(
                status=ResponseStatus.WAITING,
                run_id=run_id,
                reason="no_runnable_task",
            )

    def submit_task_result(
        self,
        run_id: str,
        task_id: str,
        *,
        result_type: str,
        result: Any = None,
        error: Mapping[str, Any] | None = None,
    ) -> SubmissionResponse:
        try:
            selected_result_type = ResultType(result_type)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in ResultType)
            raise ValidationError(f"Unknown result_type '{result_type}'. Expected one of: {allowed}") from exc

        with self.storage.transaction(immediate=True) as connection:
            run = self.storage.get_run(connection, run_id)
            self._validate_run_identity(run)
            task = self.storage.get_task(connection, run_id, task_id)
            definition = self.flow.task(task_id)

            if task["status"] == TaskStatus.RUNNING:
                return self._submit_running_task(
                    connection,
                    run,
                    task,
                    definition,
                    selected_result_type,
                    result,
                    error,
                )
            if task["status"] == TaskStatus.WAITING_INPUT:
                return self._submit_human_task(
                    connection,
                    run,
                    task,
                    definition,
                    selected_result_type,
                    result,
                )
            if task["status"] == TaskStatus.WAITING_RETRY_CONFIRMATION:
                return self._submit_retry_decision(
                    connection,
                    run,
                    task,
                    selected_result_type,
                    result,
                )
            raise InvalidStateError(
                f"Task '{task_id}' cannot accept a result while in state '{task['status']}'"
            )

    def status(self, run_id: str) -> dict[str, Any]:
        status = self.storage.read_status(run_id)
        self._validate_run_identity(status["run"])
        return status

    def events(self, run_id: str) -> list[dict[str, Any]]:
        status = self.storage.read_status(run_id)
        self._validate_run_identity(status["run"])
        return self.storage.read_events(run_id)

    def _active_task_response(
        self,
        connection: sqlite3.Connection,
        run: dict[str, Any],
        tasks: Sequence[dict[str, Any]],
    ) -> FlowResponse | None:
        for task in tasks:
            definition = self.flow.task(task["task_id"])
            if task["status"] == TaskStatus.RUNNING:
                if self.state.is_stale(task):
                    self.state.transition_task(
                        connection,
                        run_id=run["run_id"],
                        task_id=task["task_id"],
                        current_status=TaskStatus.RUNNING,
                        target_status=TaskStatus.WAITING_RETRY_CONFIRMATION,
                        event_type="task_stale_detected",
                        event_payload={
                            "started_at": task["started_at"],
                            "stale_after_seconds": task["stale_after_seconds"],
                        },
                    )
                    self._set_run_status(connection, run, RunStatus.WAITING)
                    return self._retry_confirmation_response(run, task)
                return FlowResponse(
                    status=ResponseStatus.WAITING,
                    run_id=run["run_id"],
                    reason="task_already_running",
                    task=self._running_task_payload(task, definition),
                )
            if task["status"] == TaskStatus.WAITING_RETRY_CONFIRMATION:
                return self._retry_confirmation_response(run, task)
            if task["status"] == TaskStatus.WAITING_INPUT:
                return self._human_response(run, task, definition)
        return None

    @staticmethod
    def _running_task_payload(
        task: Mapping[str, Any],
        definition: TaskDefinition,
    ) -> dict[str, Any]:
        started_at = task.get("started_at")
        elapsed_seconds = None
        if started_at:
            started = datetime.fromisoformat(str(started_at))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed_seconds = max(
                0,
                int((datetime.now(timezone.utc) - started).total_seconds()),
            )
        return {
            "task_id": task["task_id"],
            "task_name": definition.metadata.get("name", task["task_id"]),
            "started_at": started_at,
            "elapsed_seconds": elapsed_seconds,
            "stale_after_seconds": task.get("stale_after_seconds"),
            "retry_count": task.get("retry_count", 0),
        }

    def _submit_running_task(
        self,
        connection: sqlite3.Connection,
        run: dict[str, Any],
        task: dict[str, Any],
        definition: TaskDefinition,
        result_type: ResultType,
        result: Any,
        error: Mapping[str, Any] | None,
    ) -> SubmissionResponse:
        if result_type is ResultType.EXECUTION_RESULT:
            validation = self._validate_result(connection, definition, result, run)
            if not validation.accepted:
                self.storage.add_event(
                    connection,
                    run_id=run["run_id"],
                    task_id=task["task_id"],
                    event_type="task_result_rejected",
                    payload={"errors": validation.errors},
                )
                return SubmissionResponse(
                    status=SubmissionStatus.REJECTED,
                    run_id=run["run_id"],
                    task_id=task["task_id"],
                    errors=validation.errors,
                    retry_count=task["retry_count"],
                )
            self.state.transition_task(
                connection,
                run_id=run["run_id"],
                task_id=task["task_id"],
                current_status=TaskStatus.RUNNING,
                target_status=TaskStatus.COMPLETED,
                event_type="task_completed",
                outputs_json=encode_json(result),
                error_json=None,
            )
            self._set_run_status(connection, run, RunStatus.RUNNING)
            return SubmissionResponse(
                status=SubmissionStatus.ACCEPTED,
                run_id=run["run_id"],
                task_id=task["task_id"],
                retry_count=task["retry_count"],
                next_available=True,
            )

        if result_type is ResultType.EXECUTION_ERROR:
            error_payload = dict(error or {})
            if task["retry_count"] < task["max_retries"]:
                next_retry_count = task["retry_count"] + 1
                self.state.transition_task(
                    connection,
                    run_id=run["run_id"],
                    task_id=task["task_id"],
                    current_status=TaskStatus.RUNNING,
                    target_status=TaskStatus.PENDING,
                    event_type="task_retry_scheduled",
                    event_payload={"error": error_payload, "retry_count": next_retry_count},
                    retry_count=next_retry_count,
                    started_at=None,
                    finished_at=None,
                    error_json=encode_json(error_payload),
                )
                return SubmissionResponse(
                    status=SubmissionStatus.RETRY_SCHEDULED,
                    run_id=run["run_id"],
                    task_id=task["task_id"],
                    retry_count=next_retry_count,
                    next_available=True,
                )
            self.state.transition_task(
                connection,
                run_id=run["run_id"],
                task_id=task["task_id"],
                current_status=TaskStatus.RUNNING,
                target_status=TaskStatus.FAILED,
                event_type="task_failed",
                event_payload={"error": error_payload, "reason": "retries_exhausted"},
                error_json=encode_json(error_payload),
            )
            self._set_run_status(connection, run, RunStatus.FAILED)
            return SubmissionResponse(
                status=SubmissionStatus.RUN_FAILED,
                run_id=run["run_id"],
                task_id=task["task_id"],
                retry_count=task["retry_count"],
            )

        raise InvalidStateError(
            f"Running Agent Task requires '{ResultType.EXECUTION_RESULT}' or "
            f"'{ResultType.EXECUTION_ERROR}'"
        )

    def _submit_human_task(
        self,
        connection: sqlite3.Connection,
        run: dict[str, Any],
        task: dict[str, Any],
        definition: TaskDefinition,
        result_type: ResultType,
        result: Any,
    ) -> SubmissionResponse:
        allowed = {ResultType.USER_DECISION, ResultType.USER_INPUT}
        if result_type not in allowed:
            raise InvalidStateError("Human Task requires user_decision or user_input")
        validation = self._validate_result(connection, definition, result, run)
        if not validation.accepted:
            return SubmissionResponse(
                status=SubmissionStatus.REJECTED,
                run_id=run["run_id"],
                task_id=task["task_id"],
                errors=validation.errors,
            )
        self.state.transition_task(
            connection,
            run_id=run["run_id"],
            task_id=task["task_id"],
            current_status=TaskStatus.WAITING_INPUT,
            target_status=TaskStatus.COMPLETED,
            event_type="human_input_received",
            outputs_json=encode_json(result),
            error_json=None,
        )
        self._set_run_status(connection, run, RunStatus.RUNNING)
        return SubmissionResponse(
            status=SubmissionStatus.ACCEPTED,
            run_id=run["run_id"],
            task_id=task["task_id"],
            next_available=True,
        )

    def _submit_retry_decision(
        self,
        connection: sqlite3.Connection,
        run: dict[str, Any],
        task: dict[str, Any],
        result_type: ResultType,
        result: Any,
    ) -> SubmissionResponse:
        if result_type is not ResultType.USER_DECISION:
            raise InvalidStateError("Stale Task recovery requires a user_decision")
        decision = result.get("decision") if isinstance(result, Mapping) else result
        if decision == "retry":
            next_retry_count = task["retry_count"] + 1
            self.state.transition_task(
                connection,
                run_id=run["run_id"],
                task_id=task["task_id"],
                current_status=TaskStatus.WAITING_RETRY_CONFIRMATION,
                target_status=TaskStatus.PENDING,
                event_type="task_retry_confirmed",
                event_payload={"retry_count": next_retry_count},
                retry_count=next_retry_count,
                started_at=None,
                finished_at=None,
                error_json=None,
            )
            self._set_run_status(connection, run, RunStatus.RUNNING)
            return SubmissionResponse(
                status=SubmissionStatus.RETRY_SCHEDULED,
                run_id=run["run_id"],
                task_id=task["task_id"],
                retry_count=next_retry_count,
                next_available=True,
            )
        if decision == "abort":
            self.state.transition_task(
                connection,
                run_id=run["run_id"],
                task_id=task["task_id"],
                current_status=TaskStatus.WAITING_RETRY_CONFIRMATION,
                target_status=TaskStatus.FAILED,
                event_type="task_retry_rejected",
            )
            self._set_run_status(connection, run, RunStatus.FAILED)
            return SubmissionResponse(
                status=SubmissionStatus.RUN_FAILED,
                run_id=run["run_id"],
                task_id=task["task_id"],
                retry_count=task["retry_count"],
            )
        return SubmissionResponse(
            status=SubmissionStatus.REJECTED,
            run_id=run["run_id"],
            task_id=task["task_id"],
            errors=["decision must be 'retry' or 'abort'"],
            retry_count=task["retry_count"],
        )

    def _validate_result(
        self,
        connection: sqlite3.Connection,
        definition: TaskDefinition,
        result: Any,
        run: dict[str, Any],
    ) -> ValidationResult:
        if definition.validator is None:
            return ValidationResult.accept()
        tasks = self.storage.list_tasks(connection, run["run_id"])
        context = self._context(run, tasks)
        try:
            outcome = definition.validator(result, context)
        except Exception as exc:
            return ValidationResult.reject(f"Validator raised {type(exc).__name__}: {exc}")
        if outcome is None or outcome is True:
            return ValidationResult.accept()
        if outcome is False:
            return ValidationResult.reject("Result validation failed")
        if isinstance(outcome, ValidationResult):
            return outcome
        if isinstance(outcome, Sequence) and not isinstance(outcome, (str, bytes)):
            errors = [str(item) for item in outcome]
            return ValidationResult(accepted=not errors, errors=errors)
        raise ValidationError(
            f"Validator for task '{definition.id}' returned unsupported value {type(outcome).__name__}"
        )

    def _completion_gate(
        self,
        run: dict[str, Any],
        tasks: Sequence[Mapping[str, Any]],
    ) -> ValidationResult:
        if self.flow.completion_gate is None:
            return ValidationResult.accept()
        context = self._context(run, tasks)
        try:
            outcome = self.flow.completion_gate(None, context)
        except Exception as exc:
            return ValidationResult.reject(
                f"Completion gate raised {type(exc).__name__}: {exc}"
            )
        if outcome is None or outcome is True:
            return ValidationResult.accept()
        if outcome is False:
            return ValidationResult.reject("Run completion gate failed")
        if isinstance(outcome, ValidationResult):
            return outcome
        if isinstance(outcome, Sequence) and not isinstance(outcome, (str, bytes)):
            errors = [str(item) for item in outcome]
            return ValidationResult(accepted=not errors, errors=errors)
        raise ValidationError(
            f"Completion gate returned unsupported value {type(outcome).__name__}"
        )

    def _context(
        self,
        run: Mapping[str, Any],
        tasks: Sequence[Mapping[str, Any]],
    ) -> TaskContext:
        outputs = {
            str(task["task_id"]): task["outputs"]
            for task in tasks
            if task.get("outputs") is not None
        }
        return TaskContext(
            run_id=str(run["run_id"]),
            run_inputs=run["inputs"],
            task_outputs=outputs,
            workspace=Path(str(run["workspace_path"])),
        )

    def _condition_matches(self, definition: TaskDefinition, context: TaskContext) -> bool:
        if definition.when is None:
            return True
        try:
            return bool(definition.when(context))
        except Exception as exc:
            raise FlowDefinitionError(
                f"Condition for task '{definition.id}' raised {type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _dependencies_satisfied(
        task: Mapping[str, Any],
        task_map: Mapping[str, Mapping[str, Any]],
    ) -> bool:
        return all(
            task_map[dependency]["status"] in TERMINAL_TASK_STATUSES
            for dependency in task["dependencies"]
        )

    def _task_payload(
        self,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
        definition: TaskDefinition,
        all_tasks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        workspace = Path(str(run["workspace_path"]))
        task_working = workspace / "working" / str(task["task_id"])
        task_working.mkdir(parents=True, exist_ok=True)
        dependency_outputs = {
            dependency: next(
                item["outputs"] for item in all_tasks if item["task_id"] == dependency
            )
            for dependency in task["dependencies"]
        }
        return {
            "task_id": task["task_id"],
            "task_status": task["status"],
            "type": task["task_type"],
            "instruction": task["instruction"],
            "retry_count": task["retry_count"],
            "max_retries": task["max_retries"],
            "stale_after_seconds": task["stale_after_seconds"],
            "inputs": {
                "run": run["inputs"],
                "task": task["inputs"],
                "dependencies": dependency_outputs,
            },
            "workspace": {
                "root": str(workspace),
                "inputs": str(workspace / "inputs"),
                "working": str(task_working),
                "outputs": str(workspace / "outputs"),
            },
            "metadata": definition.metadata,
        }

    def _human_response(
        self,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
        definition: TaskDefinition,
    ) -> FlowResponse:
        return FlowResponse(
            status=ResponseStatus.WAITING_USER,
            run_id=str(run["run_id"]),
            task={
                "task_id": task["task_id"],
                "task_status": task["status"],
                "type": TaskType.HUMAN,
                "mode": definition.mode,
                "instruction": definition.instruction,
                "metadata": definition.metadata,
            },
            reason="human_input_required",
            prompt=definition.instruction,
        )

    @staticmethod
    def _retry_confirmation_response(
        run: Mapping[str, Any],
        task: Mapping[str, Any],
    ) -> FlowResponse:
        return FlowResponse(
            status=ResponseStatus.WAITING_USER,
            run_id=str(run["run_id"]),
            task={
                "task_id": task["task_id"],
                "task_status": task["status"],
                "started_at": task["started_at"],
                "stale_after_seconds": task["stale_after_seconds"],
                "retry_count": task["retry_count"],
                "max_retries": task["max_retries"],
            },
            reason="task_stale",
            prompt=f"任务 {task['task_id']} 已长时间处于运行状态，是否重新执行整个任务？",
        )

    def _set_run_status(
        self,
        connection: sqlite3.Connection,
        run: dict[str, Any],
        target_status: RunStatus,
    ) -> None:
        current = run["status"]
        if current == target_status:
            return
        self.state.transition_run(
            connection,
            run_id=run["run_id"],
            current_status=current,
            target_status=target_status,
        )
        run["status"] = target_status

    def _validate_run_identity(self, run: Mapping[str, Any]) -> None:
        if run["flow_name"] != self.flow.name or run["flow_version"] != self.flow.version:
            raise FlowVersionMismatchError(
                f"Run uses {run['flow_name']}@{run['flow_version']}, but loaded flow is "
                f"{self.flow.name}@{self.flow.version}"
            )
