"""Python API for defining and running a Flow-Skill workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .errors import FlowDefinitionError
from .models import (
    FlowResponse,
    InputSpec,
    OutputSpec,
    SubmissionResponse,
    TaskType,
    ValidationResult,
)

Condition = Callable[["TaskContext"], bool]
Validator = Callable[[Any, "TaskContext"], ValidationResult | bool | Sequence[str] | None]


def _normalize_after(after: str | Iterable[str] | None) -> tuple[str, ...]:
    if after is None:
        return ()
    if isinstance(after, str):
        return (after,)
    return tuple(after)


def _normalize_specs(specs: Sequence[Any] | None, label: str) -> tuple[Any, ...]:
    if specs is None:
        return ()
    if isinstance(specs, InputSpec | OutputSpec):
        specs = (specs,)
    items = tuple(specs)
    names = [item.name for item in items]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise FlowDefinitionError(f"{label} names 重复: {', '.join(duplicates)}")
    return items


@dataclass(slots=True)
class TaskDefinition:
    id: str
    task_type: TaskType
    instruction: str
    after: tuple[str, ...] = ()
    when: Condition | None = None
    validator: Validator | None = None
    stale_after: int | None = 1800
    # V1 已暂时关闭失败自动重试，参数保留仅为兼容，暂不生效。
    max_retries: int = 0
    mode: str | None = None
    inputs: tuple[InputSpec, ...] = ()
    outputs: tuple[OutputSpec, ...] = ()
    max_validation_retries: int = 3
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise FlowDefinitionError("Task id cannot be empty")
        if self.stale_after is not None and self.stale_after <= 0:
            raise FlowDefinitionError("stale_after must be greater than zero or None")
        if self.max_retries < 0:
            raise FlowDefinitionError("max_retries cannot be negative")
        if self.max_validation_retries < 0:
            raise FlowDefinitionError("max_validation_retries cannot be negative")
        if self.task_type is TaskType.HUMAN and self.mode not in {"approval", "input"}:
            raise FlowDefinitionError("Human task mode must be 'approval' or 'input'")


class TaskContext:
    """Read-only view of persisted run inputs and prior task outputs."""

    def __init__(
        self,
        *,
        run_id: str,
        run_inputs: Mapping[str, Any],
        task_outputs: Mapping[str, Any],
        workspace: Path,
        base_dir: Path | None = None,
    ) -> None:
        self.run_id = run_id
        self.inputs = dict(run_inputs)
        self.outputs = dict(task_outputs)
        self.workspace = workspace
        self.base_dir = base_dir

    def output(self, task_id: str, key: str | None = None, default: Any = None) -> Any:
        value = self.outputs.get(task_id, default)
        if key is None or not isinstance(value, Mapping):
            return value
        return value.get(key, default)

    def resolve(self, path: str | Path) -> Path:
        """文件路径契约 §7：相对路径 → workspace 内绝对路径；绝对路径原样返回。"""
        from .paths import resolve_workspace_path

        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return resolve_workspace_path(self.workspace, str(candidate))

    def is_within_workspace(self, path: str | Path) -> bool:
        """文件路径契约 §7：symlink 感知的 workspace 内检查。"""
        from .paths import is_within_workspace

        return is_within_workspace(self.workspace, path)


class Flow:
    """Ordered task graph and convenience facade for the Flow-Skill SDK."""

    def __init__(
        self,
        name: str,
        *,
        version: str = "1.0",
        completion_gate: Validator | None = None,
        base_dir: str | Path | None = None,
        default_data_dir: str | Path | None = None,
    ) -> None:
        if not name or not name.strip():
            raise FlowDefinitionError("Flow name cannot be empty")
        if not version or not version.strip():
            raise FlowDefinitionError("Flow version cannot be empty")
        self.name = name
        self.version = version
        self.completion_gate = completion_gate
        self.base_dir = Path(base_dir).resolve() if base_dir is not None else None
        self.default_data_dir = default_data_dir
        self.skill_path: Path | None = None
        self._tasks: dict[str, TaskDefinition] = {}

    @property
    def tasks(self) -> tuple[TaskDefinition, ...]:
        return tuple(self._tasks.values())

    def agent(
        self,
        *,
        id: str,
        instruction: str,
        after: str | Iterable[str] | None = None,
        when: Condition | None = None,
        validator: Validator | None = None,
        stale_after: int | None = 1800,
        max_retries: int = 0,
        inputs: Sequence[InputSpec] | None = None,
        outputs: Sequence[OutputSpec] | None = None,
        max_validation_retries: int = 3,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Flow":
        self._add_task(
            TaskDefinition(
                id=id,
                task_type=TaskType.AGENT,
                instruction=instruction,
                after=_normalize_after(after),
                when=when,
                validator=validator,
                stale_after=stale_after,
                max_retries=max_retries,
                inputs=_normalize_specs(inputs, f"Task '{id}' inputs"),
                outputs=_normalize_specs(outputs, f"Task '{id}' outputs"),
                max_validation_retries=max_validation_retries,
                metadata=dict(metadata or {}),
            )
        )
        return self

    def human(
        self,
        *,
        id: str,
        instruction: str,
        after: str | Iterable[str] | None = None,
        when: Condition | None = None,
        mode: str = "approval",
        validator: Validator | None = None,
        inputs: Sequence[InputSpec] | None = None,
        outputs: Sequence[OutputSpec] | None = None,
        max_validation_retries: int = 3,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Flow":
        self._add_task(
            TaskDefinition(
                id=id,
                task_type=TaskType.HUMAN,
                instruction=instruction,
                after=_normalize_after(after),
                when=when,
                validator=validator,
                stale_after=None,
                mode=mode,
                inputs=_normalize_specs(inputs, f"Task '{id}' inputs"),
                outputs=_normalize_specs(outputs, f"Task '{id}' outputs"),
                max_validation_retries=max_validation_retries,
                metadata=dict(metadata or {}),
            )
        )
        return self

    def validate(self) -> None:
        if not self._tasks:
            raise FlowDefinitionError("Flow must define at least one task")
        for task in self._tasks.values():
            if task.task_type is TaskType.AGENT and task.validator is None:
                raise FlowDefinitionError(
                    f"Agent task '{task.id}' must define a validator completion gate"
                )
            unknown = [dependency for dependency in task.after if dependency not in self._tasks]
            if unknown:
                raise FlowDefinitionError(
                    f"Task '{task.id}' has unknown dependencies: {', '.join(unknown)}"
                )
            if task.id in task.after:
                raise FlowDefinitionError(f"Task '{task.id}' cannot depend on itself")
            self._validate_input_contract(task)
            self._validate_output_contract(task)
        self._validate_shared_workbooks()
        self._validate_acyclic()

    def task(self, task_id: str) -> TaskDefinition:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise FlowDefinitionError(f"Unknown task '{task_id}'") from exc

    # -- run facade ---------------------------------------------------------

    def create_run(
        self,
        *,
        inputs: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        data_dir: str | Path | None = None,
        inputs_file: str | Path | None = None,
    ) -> str:
        from .registry import resolve_create_data_dir
        from .scheduler import Scheduler

        resolved_dir = resolve_create_data_dir(self, data_dir)
        return Scheduler(self, data_dir=resolved_dir).create_run(
            inputs=dict(inputs or {}),
            run_id=run_id,
            inputs_file=inputs_file,
        )

    def get_next_task(
        self,
        run_id: str,
        *,
        data_dir: str | Path | None = None,
        rebind: bool = False,
    ) -> FlowResponse:
        from .registry import resolve_resume_data_dir
        from .scheduler import Scheduler

        resolved_dir, _ = resolve_resume_data_dir(self, run_id, data_dir, rebind=rebind)
        return Scheduler(self, data_dir=resolved_dir).get_next_task(run_id)

    def submit_task_result(
        self,
        run_id: str,
        task_id: str,
        *,
        result_type: str,
        result: Any = None,
        error: Mapping[str, Any] | None = None,
        data_dir: str | Path | None = None,
        rebind: bool = False,
    ) -> SubmissionResponse:
        from .registry import resolve_resume_data_dir
        from .scheduler import Scheduler

        resolved_dir, _ = resolve_resume_data_dir(self, run_id, data_dir, rebind=rebind)
        return Scheduler(self, data_dir=resolved_dir).submit_task_result(
            run_id,
            task_id,
            result_type=result_type,
            result=result,
            error=dict(error or {}) if error is not None else None,
        )

    def status(
        self,
        run_id: str,
        *,
        data_dir: str | Path | None = None,
        rebind: bool = False,
    ) -> dict[str, Any]:
        from .registry import resolve_resume_data_dir
        from .scheduler import Scheduler

        resolved_dir, _ = resolve_resume_data_dir(self, run_id, data_dir, rebind=rebind)
        return Scheduler(self, data_dir=resolved_dir).status(run_id)

    def events(
        self,
        run_id: str,
        *,
        data_dir: str | Path | None = None,
        rebind: bool = False,
    ) -> list[dict[str, Any]]:
        from .registry import resolve_resume_data_dir
        from .scheduler import Scheduler

        resolved_dir, _ = resolve_resume_data_dir(self, run_id, data_dir, rebind=rebind)
        return Scheduler(self, data_dir=resolved_dir).events(run_id)

    def cli(self, argv: Sequence[str] | None = None) -> int:
        from .cli import run_flow_cli

        return run_flow_cli(self, argv=argv)

    # -- definition-time contract validation --------------------------------

    def _validate_input_contract(self, task: TaskDefinition) -> None:
        for spec in task.inputs:
            if spec.kind not in ("text", "json", "file"):
                raise FlowDefinitionError(
                    f"Task '{task.id}' input '{spec.name}' has unknown kind '{spec.kind}'"
                )
            if spec.source == "other_task":
                upstream_id = spec.from_task
                if upstream_id not in self._tasks:
                    raise FlowDefinitionError(
                        f"Task '{task.id}' input '{spec.name}' 引用了不存在的任务 '{upstream_id}'"
                    )
                if upstream_id not in task.after:
                    raise FlowDefinitionError(
                        f"Task '{task.id}' input '{spec.name}' 的 from_task='{upstream_id}' "
                        f"必须同时出现在 after 中（数据依赖 ⊆ 执行依赖）"
                    )
                upstream = self._tasks[upstream_id]
                self._check_output_reference(task, spec, upstream)
                if spec.kind == "file":
                    self._check_sheet_ownership(task, spec, upstream)
            if spec.source == "review" and spec.from_task is not None:
                raise FlowDefinitionError(
                    f"Task '{task.id}' input '{spec.name}': source='review' 不接受 from_task"
                )

    def _check_output_reference(
        self,
        task: TaskDefinition,
        spec: InputSpec,
        upstream: TaskDefinition,
    ) -> None:
        wanted_kind = "file" if spec.kind == "file" else "json"
        candidates = [item for item in upstream.outputs if item.kind == wanted_kind]
        if spec.output is not None:
            if spec.output not in {item.name for item in upstream.outputs}:
                raise FlowDefinitionError(
                    f"Task '{task.id}' input '{spec.name}' 引用的上游输出 "
                    f"'{spec.output}' 不存在于任务 '{upstream.id}'"
                )
            return
        if len(candidates) > 1:
            raise FlowDefinitionError(
                f"Task '{task.id}' input '{spec.name}': 上游任务 '{upstream.id}' 有多个 "
                f"{wanted_kind} 输出，必须用 output 参数指明"
            )
        # zero candidates → upstream undeclared; assembly falls back to the
        # whole upstream result (mixed declared/undeclared compat mode)

    def _check_sheet_ownership(
        self,
        task: TaskDefinition,
        spec: InputSpec,
        upstream: TaskDefinition,
    ) -> None:
        wanted = spec.sheet if spec.sheet is not None else None
        wanted_list = spec.sheets if spec.sheets else ([wanted] if wanted else [])
        if not wanted_list:
            return
        upstream_xlsx = [
            item
            for item in upstream.outputs
            if item.kind == "file"
            and (item.format == "xlsx")
            and (spec.output is None or item.name == spec.output)
        ]
        declared_sheets: set[str] = set()
        for item in upstream_xlsx:
            for entry in (item.metadata or {}).get("sheets", []) or []:
                declared_sheets.add(entry["name"] if isinstance(entry, Mapping) else str(entry))
        for sheet_name in wanted_list:
            if not sheet_name.startswith(f"{upstream.id}__"):
                raise FlowDefinitionError(
                    f"Task '{task.id}' input '{spec.name}': sheet '{sheet_name}' 不属于"
                    f"上游任务 '{upstream.id}'（缺少前缀 {upstream.id}__）"
                )
            if declared_sheets and sheet_name not in declared_sheets:
                raise FlowDefinitionError(
                    f"Task '{task.id}' input '{spec.name}': sheet '{sheet_name}' 不在上游"
                    f"任务 '{upstream.id}' 声明的 sheets 契约内"
                )

    def _validate_output_contract(self, task: TaskDefinition) -> None:
        for spec in task.outputs:
            if spec.kind == "file":
                for entry in (spec.metadata or {}).get("sheets", []) or []:
                    sheet_name = entry["name"] if isinstance(entry, Mapping) else str(entry)
                    if not sheet_name.startswith(f"{task.id}__"):
                        raise FlowDefinitionError(
                            f"Task '{task.id}' output '{spec.name}': sheet '{sheet_name}' "
                            f"必须以 '{task.id}__' 为前缀（sheet 所有权规则）"
                        )

    def _validate_shared_workbooks(self) -> None:
        by_path: dict[str, set[str]] = {}
        for task in self._tasks.values():
            for spec in task.outputs:
                if spec.kind == "file" and spec.path:
                    by_path.setdefault(spec.path, set()).add(spec.format or "")
        for path, formats in by_path.items():
            if len(formats) > 1:
                raise FlowDefinitionError(
                    f"路径 '{path}' 被声明为多种 format（{sorted(formats)}）："
                    f"共享工作簿的 format 必须一致"
                )

    # -- internal -----------------------------------------------------------

    def _add_task(self, task: TaskDefinition) -> None:
        if task.id in self._tasks:
            raise FlowDefinitionError(f"Duplicate task id '{task.id}'")
        self._tasks[task.id] = task

    def _validate_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visited:
                return
            if task_id in visiting:
                raise FlowDefinitionError(f"Dependency cycle detected at task '{task_id}'")
            visiting.add(task_id)
            for dependency in self._tasks[task_id].after:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in self._tasks:
            visit(task_id)
