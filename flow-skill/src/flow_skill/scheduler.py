"""Task scheduling, branching, stale recovery, and result submission."""

from __future__ import annotations

import re
import shutil
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
    InputSpec,
    ResponseStatus,
    ResultType,
    RunStatus,
    SubmissionResponse,
    SubmissionStatus,
    TaskStatus,
    TaskType,
    ValidationResult,
)
from .registry import Registry
from .schema import verify_output_contract, xlsx_sheet_names
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

    # -- helpers ------------------------------------------------------------

    def _resolve_instruction(self, raw: str) -> str:
        """文件路径契约 §4.2：相对 instruction 经 base_dir 解析为绝对路径。"""
        if self.flow.base_dir is None:
            return raw
        candidate = Path(raw)
        if candidate.is_absolute():
            return str(candidate)
        return str(self.flow.base_dir / candidate)

    def _registry(self) -> Registry:
        return Registry.for_flow(self.flow)

    def _probe_writable(self) -> None:
        probe = self.flow_data_dir / ".write_probe"
        try:
            self.flow_data_dir.mkdir(parents=True, exist_ok=True)
            probe.write_text("probe", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise ValidationError(
                f"数据目录 {self.flow_data_dir} 不可写（{exc}）。"
                f"请用 --data-dir 指定当前环境可访问的持久目录"
            ) from exc

    def create_run(
        self,
        *,
        inputs: Mapping[str, Any],
        run_id: str | None = None,
        inputs_file: str | Path | None = None,
    ) -> str:
        selected_run_id = run_id or f"run_{uuid.uuid4().hex}"
        if not SAFE_ID.fullmatch(selected_run_id):
            raise ValidationError(
                "run_id may contain only letters, numbers, dot, underscore, and hyphen"
            )

        registry = self._registry()
        record = registry.lookup(selected_run_id) if registry.available else None
        if record is not None:
            raise ValidationError(
                f"run_id '{selected_run_id}' 已注册于 {record['data_dir']}，禁止重复 create-run"
            )

        self._probe_writable()

        inputs_dict = dict(inputs)
        inputs_source: str | None = None
        if inputs_file is not None:
            source_path = Path(inputs_file)
            if not source_path.is_file():
                raise ValidationError(f"inputs_file 不存在: {source_path}")

        workspace = (self.flow_data_dir / "runs" / selected_run_id).resolve()
        expected_parent = (self.flow_data_dir / "runs").resolve()
        if workspace.parent != expected_parent:
            raise ValidationError("Resolved workspace is outside the flow run directory")
        for folder in ("inputs", "working", "outputs"):
            (workspace / folder).mkdir(parents=True, exist_ok=True)

        if inputs_file is not None:
            source_path = Path(inputs_file)
            destination = workspace / "inputs" / source_path.name
            if destination.exists():
                raise ValidationError(f"workspace/inputs 中已存在同名文件: {source_path.name}")
            shutil.copyfile(source_path, destination)
            inputs_source = f"inputs/{source_path.name}"
            inputs_dict = {**inputs_dict, "inputs_source": inputs_source}

        tasks = [self._task_snapshot(task) for task in self.flow.tasks]
        try:
            with self.storage.transaction(immediate=True) as connection:
                self.storage.create_run(
                    connection,
                    run_id=selected_run_id,
                    flow_name=self.flow.name,
                    flow_version=self.flow.version,
                    inputs=inputs_dict,
                    workspace_path=workspace,
                    tasks=tasks,
                    skill_path=str(self.flow.skill_path) if self.flow.skill_path else None,
                    inputs_source=inputs_source,
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"Run '{selected_run_id}' already exists") from exc

        if registry.available:
            registry.register(
                run_id=selected_run_id,
                data_dir=self.data_dir,
                flow_name=self.flow.name,
                flow_version=self.flow.version,
                skill_path=str(self.flow.skill_path) if self.flow.skill_path else None,
            )
        return selected_run_id

    def _task_snapshot(self, task: TaskDefinition) -> dict[str, Any]:
        resolved_instruction = self._resolve_instruction(task.instruction)
        return {
            "id": task.id,
            "task_type": task.task_type,
            "instruction": resolved_instruction,
            "instruction_raw": task.instruction,
            "after": task.after,
            "mode": task.mode,
            "metadata": task.metadata,
            "max_retries": task.max_retries,
            "stale_after": task.stale_after,
            "inputs_spec_json": (
                encode_json([spec.to_dict() for spec in task.inputs]) if task.inputs else None
            ),
            "outputs_spec_json": (
                encode_json([spec.to_dict() for spec in task.outputs]) if task.outputs else None
            ),
            "max_validation_retries": task.max_validation_retries,
        }

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

                    # 级联跳过：required 输入的来源任务被 skipped（结构性缺失）
                    skipped_source = self._required_source_skipped(definition, task_map)
                    if skipped_source is not None:
                        self.state.transition_task(
                            connection,
                            run_id=run_id,
                            task_id=task["task_id"],
                            current_status=TaskStatus.PENDING,
                            target_status=TaskStatus.SKIPPED,
                            event_type="task_skipped",
                            event_payload={
                                "reason": "required_input_skipped",
                                "skipped_source": skipped_source,
                            },
                        )
                        progressed = True
                        break

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

                    # 领取期装配（在状态迁移前完成，失败走重试通道）
                    assembled, assembly_errors = self._assemble_inputs(run, task, definition, task_map)
                    if assembly_errors:
                        return self._handle_assembly_failure(
                            connection, run, task, definition, assembly_errors
                        )

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
                        return self._human_response(run, task, definition, assembled)

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
                        task=self._task_payload(run, refreshed, definition, tasks, assembled),
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
            errors = self._contract_errors(definition, result, run, task)
            validation = self._validate_result(connection, definition, result, run)
            if not validation.accepted:
                errors = errors + list(validation.errors)
            if errors:
                return self._handle_rejection(connection, run, task, definition, errors)
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
            self.storage.update_task(
                connection,
                run["run_id"],
                task["task_id"],
                validation_retry_count=0,
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
            # V1 已暂时关闭失败自动重试：执行错误直接导致 Task 和 Run 失败。
            error_payload = dict(error or {})
            self.state.transition_task(
                connection,
                run_id=run["run_id"],
                task_id=task["task_id"],
                current_status=TaskStatus.RUNNING,
                target_status=TaskStatus.FAILED,
                event_type="task_failed",
                event_payload={"error": error_payload, "reason": "retry_disabled"},
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
        if definition.mode == "approval" and result_type is not ResultType.USER_DECISION:
            raise InvalidStateError(
                f"Human Task '{task['task_id']}' (mode='approval') 只接受 user_decision"
            )
        if definition.mode == "input" and result_type is not ResultType.USER_INPUT:
            raise InvalidStateError(
                f"Human Task '{task['task_id']}' (mode='input') 只接受 user_input"
            )
        errors = self._contract_errors(definition, result, run, task)
        validation = self._validate_result(connection, definition, result, run)
        if not validation.accepted:
            errors = errors + list(validation.errors)
        if errors:
            return self._handle_rejection(connection, run, task, definition, errors)
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
        self.storage.update_task(
            connection,
            run["run_id"],
            task["task_id"],
            validation_retry_count=0,
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

    # -- contract assembly & verification -----------------------------------

    def _required_source_skipped(
        self,
        definition: TaskDefinition,
        task_map: Mapping[str, Mapping[str, Any]],
    ) -> str | None:
        for spec in definition.inputs:
            if spec.kind in ("json", "file") and spec.source == "other_task" and spec.required:
                upstream = task_map.get(spec.from_task or "")
                if upstream is not None and upstream["status"] == TaskStatus.SKIPPED:
                    return str(spec.from_task)
        return None

    def _assemble_inputs(
        self,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
        definition: TaskDefinition,
        task_map: Mapping[str, Mapping[str, Any]],
    ) -> tuple[dict[str, Any], list[str]]:
        """领取期装配：按声明拍平（输入输出契约设计 §5.1）。"""
        if not definition.inputs:
            return {}, []
        workspace = Path(str(run["workspace_path"]))
        values: dict[str, Any] = {}
        errors: list[str] = []
        for spec in definition.inputs:
            value, meta, spec_errors = self._assemble_spec(spec, run, task_map, workspace)
            if spec_errors:
                errors.extend(spec_errors)
                continue
            values[spec.name] = value
            if meta:
                values[f"{spec.name}_meta"] = meta
        return values, errors

    def _assemble_spec(
        self,
        spec: InputSpec,
        run: Mapping[str, Any],
        task_map: Mapping[str, Mapping[str, Any]],
        workspace: Path,
    ) -> tuple[Any, dict[str, Any] | None, list[str]]:
        if spec.kind == "text":
            if spec.source is None:
                return spec.value, None, []
            raw = (run["inputs"] or {}).get(spec.key or spec.name)
            if raw is None:
                if spec.required:
                    return None, None, [f"输入 '{spec.name}'：Run inputs 缺少字段"]
                return None, None, []
            if not isinstance(raw, str):
                return None, None, [
                    f"输入 '{spec.name}'：应为字符串，实为 {type(raw).__name__}"
                ]
            return raw, None, []

        if spec.kind == "json":
            if spec.source == "run_inputs":
                raw = (run["inputs"] or {}).get(spec.key or spec.name)
                if raw is None:
                    if spec.required:
                        return None, None, [f"输入 '{spec.name}'：Run inputs 缺少字段"]
                    return None, None, []
                if not isinstance(raw, (dict, list, int, float, bool)):
                    return None, None, [
                        f"输入 '{spec.name}'：应为结构化 JSON，实为 {type(raw).__name__}"
                    ]
                return raw, None, []
            # source == "other_task"
            upstream = task_map.get(spec.from_task or "")
            if upstream is None:
                return None, None, [f"输入 '{spec.name}'：上游任务 '{spec.from_task}' 不存在"]
            value = upstream.get("outputs")
            declared = [
                item
                for item in self.flow.task(str(upstream["task_id"])).outputs
                if item.kind == "json"
            ]
            if declared:
                output_name = spec.output or (
                    declared[0].name if len(declared) == 1 else None
                )
                if output_name is None:
                    return None, None, [
                        f"输入 '{spec.name}'：上游任务 '{spec.from_task}' 有多个 json 输出，"
                        f"必须用 output 参数指明"
                    ]
                if value is None:
                    if spec.required:
                        return None, None, [
                            f"输入 '{spec.name}'：上游任务 '{spec.from_task}' 未产出任何内容"
                        ]
                    return None, None, []
                value = value.get(output_name)
            if spec.key and isinstance(value, Mapping):
                value = value.get(spec.key)
            if value is None:
                if spec.required:
                    return None, None, [
                        f"输入 '{spec.name}'：上游任务 '{spec.from_task}' 未产出"
                        + (f" '{spec.output}'" if spec.output else " 对应内容")
                    ]
                return None, None, []
            if not isinstance(value, (dict, list, int, float, bool)):
                return None, None, [
                    f"输入 '{spec.name}'：上游产出应为结构化 JSON，实为 {type(value).__name__}"
                ]
            return value, None, []

        # spec.kind == "file"
        upstream = task_map.get(spec.from_task or "")
        if upstream is None:
            return None, None, [f"输入 '{spec.name}'：上游任务 '{spec.from_task}' 不存在"]
        upstream_def = self.flow.task(str(upstream["task_id"]))
        file_outputs = [item for item in upstream_def.outputs if item.kind == "file"]
        path_str: str | None = None
        declared_format: str | None = None
        declared_sheets: list[str] | None = None
        if file_outputs:
            chosen = None
            if spec.output:
                chosen = next(
                    (item for item in file_outputs if item.name == spec.output), None
                )
                if chosen is None:
                    return None, None, [
                        f"输入 '{spec.name}'：上游任务 '{spec.from_task}' 无名为 "
                        f"'{spec.output}' 的文件输出"
                    ]
            elif len(file_outputs) == 1:
                chosen = file_outputs[0]
            if chosen is not None:
                path_str = chosen.path
                declared_format = chosen.format
                if chosen.format == "xlsx":
                    declared_sheets = [
                        entry["name"] if isinstance(entry, Mapping) else str(entry)
                        for entry in (chosen.metadata or {}).get("sheets", []) or []
                    ]
        if path_str is None:
            outputs = upstream.get("outputs") or {}
            if spec.output:
                candidate = outputs.get(spec.output) if isinstance(outputs, Mapping) else None
                if isinstance(candidate, str):
                    path_str = candidate
            elif isinstance(outputs, Mapping):
                strings = [item for item in outputs.values() if isinstance(item, str)]
                if len(strings) == 1:
                    path_str = strings[0]
            if not path_str:
                if spec.required:
                    return None, None, [
                        f"输入 '{spec.name}'：无法从上游任务 '{spec.from_task}' 解析文件路径"
                        f"（上游未声明 outputs 时请在 input 上指定 output 参数）"
                    ]
                return None, None, []
        from .paths import resolve_workspace_path

        absolute = resolve_workspace_path(workspace, path_str)
        if not absolute.exists():
            return None, None, [f"输入 '{spec.name}'：文件不存在 {path_str}"]
        wanted_formats = spec.format or ((declared_format,) if declared_format else ())
        if wanted_formats and absolute.suffix.lower().lstrip(".") not in wanted_formats:
            return None, None, [
                f"输入 '{spec.name}'：扩展名 {absolute.suffix} 不在允许格式 "
                f"{list(wanted_formats)} 内"
            ]
        meta: dict[str, Any] = {
            "kind": "file",
            "format": declared_format or absolute.suffix.lower().lstrip("."),
        }
        if absolute.suffix.lower() == ".xlsx" and (spec.sheet or spec.sheets):
            try:
                actual_sheets = xlsx_sheet_names(absolute)
            except Exception as exc:  # noqa: BLE001
                return None, None, [f"输入 '{spec.name}'：xlsx 解析失败: {exc}"]
            wanted = [spec.sheet] if spec.sheet else list(spec.sheets)
            for sheet_name in wanted:
                if sheet_name not in actual_sheets:
                    return None, None, [f"输入 '{spec.name}'：缺少声明 sheet '{sheet_name}'"]
            meta["sheets"] = actual_sheets
        elif declared_sheets is not None:
            meta["sheets"] = declared_sheets
        return str(absolute), meta, []

    def _handle_assembly_failure(
        self,
        connection: sqlite3.Connection,
        run: dict[str, Any],
        task: Mapping[str, Any],
        definition: TaskDefinition,
        errors: list[str],
    ) -> FlowResponse:
        new_count = int(task.get("validation_retry_count") or 0) + 1
        limit = definition.max_validation_retries
        if new_count >= limit:
            self.state.transition_task(
                connection,
                run_id=run["run_id"],
                task_id=task["task_id"],
                current_status=TaskStatus.PENDING,
                target_status=TaskStatus.FAILED,
                event_type="task_failed",
                event_payload={
                    "reason": "validation_retries_exhausted",
                    "errors": errors,
                },
                error_json=encode_json({"stage": "input_assembly", "errors": errors}),
            )
            self._set_run_status(connection, run, RunStatus.FAILED)
            return FlowResponse(
                status=ResponseStatus.FAILED,
                run_id=str(run["run_id"]),
                reason="input_validation_failed",
                task={
                    "task_id": task["task_id"],
                    "errors": errors,
                    "validation_retry_count": new_count,
                },
            )
        self.storage.update_task(
            connection,
            run["run_id"],
            task["task_id"],
            validation_retry_count=new_count,
            error_json=encode_json({"stage": "input_assembly", "errors": errors}),
        )
        self.storage.add_event(
            connection,
            run_id=run["run_id"],
            task_id=task["task_id"],
            event_type="input_validation_failed",
            payload={"errors": errors, "retry_count": new_count},
        )
        self._set_run_status(connection, run, RunStatus.WAITING)
        return FlowResponse(
            status=ResponseStatus.WAITING,
            run_id=str(run["run_id"]),
            reason="input_validation_failed",
            task={
                "task_id": task["task_id"],
                "errors": errors,
                "validation_retry_count": new_count,
            },
        )

    def _contract_errors(
        self,
        definition: TaskDefinition,
        result: Any,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
    ) -> list[str]:
        if not definition.outputs:
            return []
        return verify_output_contract(
            output_specs=definition.outputs,
            result=result,
            workspace=Path(str(run["workspace_path"])),
            task_id=str(task["task_id"]),
            declared_tasks_for_path=self._tasks_declaring_path(str(task["task_id"])),
        )

    def _tasks_declaring_path(self, task_id: str) -> list[str]:
        definition = self.flow.task(task_id)
        paths = {
            item.path for item in definition.outputs if item.kind == "file" and item.path
        }
        return [
            candidate.id
            for candidate in self.flow.tasks
            if any(
                item.kind == "file" and item.path in paths
                for item in candidate.outputs
            )
        ]

    def _handle_rejection(
        self,
        connection: sqlite3.Connection,
        run: dict[str, Any],
        task: Mapping[str, Any],
        definition: TaskDefinition,
        errors: list[str],
    ) -> SubmissionResponse:
        """提交被拒：错误回流给 AI 重试，达到上限才失败（输入输出契约设计 §5.4）。"""
        new_count = int(task.get("validation_retry_count") or 0) + 1
        limit = definition.max_validation_retries
        if new_count >= limit:
            self.state.transition_task(
                connection,
                run_id=run["run_id"],
                task_id=task["task_id"],
                current_status=TaskStatus.RUNNING,
                target_status=TaskStatus.FAILED,
                event_type="task_failed",
                event_payload={
                    "reason": "validation_retries_exhausted",
                    "errors": errors,
                },
                error_json=encode_json({"stage": "submit", "errors": errors}),
            )
            self._set_run_status(connection, run, RunStatus.FAILED)
            return SubmissionResponse(
                status=SubmissionStatus.RUN_FAILED,
                run_id=run["run_id"],
                task_id=task["task_id"],
                errors=errors,
                retry_count=new_count,
            )
        self.storage.update_task(
            connection,
            run["run_id"],
            task["task_id"],
            validation_retry_count=new_count,
            error_json=encode_json({"stage": "submit", "errors": errors}),
        )
        self.storage.add_event(
            connection,
            run_id=run["run_id"],
            task_id=task["task_id"],
            event_type="task_result_rejected",
            payload={"errors": errors, "validation_retry_count": new_count},
        )
        return SubmissionResponse(
            status=SubmissionStatus.REJECTED,
            run_id=run["run_id"],
            task_id=task["task_id"],
            errors=errors,
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
            base_dir=self.flow.base_dir,
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
        assembled: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        workspace = Path(str(run["workspace_path"]))
        task_working = workspace / "working" / str(task["task_id"])
        task_working.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "task_id": task["task_id"],
            "task_status": task["status"],
            "type": task["task_type"],
            "instruction": task["instruction"],
            "retry_count": task["retry_count"],
            "max_retries": task["max_retries"],
            "stale_after_seconds": task["stale_after_seconds"],
            "workspace": {
                "root": str(workspace),
                "inputs": str(workspace / "inputs"),
                "working": str(task_working),
                "outputs": str(workspace / "outputs"),
            },
            "metadata": definition.metadata,
        }
        if definition.inputs:
            payload["inputs"] = dict(assembled or {})
        else:
            dependency_outputs = {
                dependency: next(
                    item["outputs"] for item in all_tasks if item["task_id"] == dependency
                )
                for dependency in task["dependencies"]
            }
            payload["inputs"] = {
                "run": run["inputs"],
                "task": task["inputs"],
                "dependencies": dependency_outputs,
            }
        if definition.outputs:
            payload["outputs_spec"] = [item.to_dict() for item in definition.outputs]
        validation_retry_count = int(task.get("validation_retry_count") or 0)
        if validation_retry_count:
            payload["validation_retry_count"] = validation_retry_count
            error_payload = task.get("error")
            if isinstance(error_payload, Mapping) and error_payload.get("errors"):
                payload["last_validation_errors"] = list(error_payload["errors"])
        return payload

    def _human_response(
        self,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
        definition: TaskDefinition,
        assembled: Mapping[str, Any] | None = None,
    ) -> FlowResponse:
        human_task: dict[str, Any] = {
            "task_id": task["task_id"],
            "task_status": task["status"],
            "type": TaskType.HUMAN,
            "mode": definition.mode,
            "instruction": definition.instruction,
            "metadata": definition.metadata,
        }
        if definition.inputs:
            human_task["inputs"] = dict(assembled or {})
        if definition.outputs:
            human_task["outputs_spec"] = [item.to_dict() for item in definition.outputs]
        return FlowResponse(
            status=ResponseStatus.WAITING_USER,
            run_id=str(run["run_id"]),
            task=human_task,
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
        run_skill = run.get("skill_path")
        flow_skill = str(self.flow.skill_path) if self.flow.skill_path else None
        if run_skill and flow_skill and Path(run_skill) != Path(flow_skill):
            raise FlowDefinitionError(
                f"flow 名 '{self.flow.name}' 与另一技能冲突（skill_path 指纹不一致）："
                f"数据库属于 {run_skill}，当前加载 {flow_skill}。"
                f"请修改 flow 名或指定独立 data-dir"
            )
