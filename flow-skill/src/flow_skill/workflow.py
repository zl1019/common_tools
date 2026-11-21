"""Python API for defining and running a Flow-Skill workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .errors import FlowDefinitionError
from .models import FlowResponse, SubmissionResponse, TaskType, ValidationResult

Condition = Callable[["TaskContext"], bool]
Validator = Callable[[Any, "TaskContext"], ValidationResult | bool | Sequence[str] | None]


def _normalize_after(after: str | Iterable[str] | None) -> tuple[str, ...]:
    if after is None:
        return ()
    if isinstance(after, str):
        return (after,)
    return tuple(after)


@dataclass(slots=True)
class TaskDefinition:
    id: str
    task_type: TaskType
    instruction: str
    after: tuple[str, ...] = ()
    when: Condition | None = None
    validator: Validator | None = None
    stale_after: int | None = 1800
    max_retries: int = 0
    mode: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise FlowDefinitionError("Task id cannot be empty")
        if self.stale_after is not None and self.stale_after <= 0:
            raise FlowDefinitionError("stale_after must be greater than zero or None")
        if self.max_retries < 0:
            raise FlowDefinitionError("max_retries cannot be negative")
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
    ) -> None:
        self.run_id = run_id
        self.inputs = dict(run_inputs)
        self.outputs = dict(task_outputs)
        self.workspace = workspace

    def output(self, task_id: str, key: str | None = None, default: Any = None) -> Any:
        value = self.outputs.get(task_id, default)
        if key is None or not isinstance(value, Mapping):
            return value
        return value.get(key, default)


class Flow:
    """Ordered task graph and convenience facade for the Flow-Skill SDK."""

    def __init__(
        self,
        name: str,
        *,
        version: str = "1.0",
        completion_gate: Validator | None = None,
    ) -> None:
        if not name or not name.strip():
            raise FlowDefinitionError("Flow name cannot be empty")
        if not version or not version.strip():
            raise FlowDefinitionError("Flow version cannot be empty")
        self.name = name
        self.version = version
        self.completion_gate = completion_gate
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
        self._validate_acyclic()

    def task(self, task_id: str) -> TaskDefinition:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise FlowDefinitionError(f"Unknown task '{task_id}'") from exc

    def create_run(
        self,
        *,
        inputs: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        data_dir: str | Path = ".flow-data",
    ) -> str:
        from .scheduler import Scheduler

        return Scheduler(self, data_dir=data_dir).create_run(inputs=dict(inputs or {}), run_id=run_id)

    def get_next_task(
        self,
        run_id: str,
        *,
        data_dir: str | Path = ".flow-data",
    ) -> FlowResponse:
        from .scheduler import Scheduler

        return Scheduler(self, data_dir=data_dir).get_next_task(run_id)

    def submit_task_result(
        self,
        run_id: str,
        task_id: str,
        *,
        result_type: str,
        result: Any = None,
        error: Mapping[str, Any] | None = None,
        data_dir: str | Path = ".flow-data",
    ) -> SubmissionResponse:
        from .scheduler import Scheduler

        return Scheduler(self, data_dir=data_dir).submit_task_result(
            run_id,
            task_id,
            result_type=result_type,
            result=result,
            error=dict(error or {}) if error is not None else None,
        )

    def status(self, run_id: str, *, data_dir: str | Path = ".flow-data") -> dict[str, Any]:
        from .scheduler import Scheduler

        return Scheduler(self, data_dir=data_dir).status(run_id)

    def events(self, run_id: str, *, data_dir: str | Path = ".flow-data") -> list[dict[str, Any]]:
        from .scheduler import Scheduler

        return Scheduler(self, data_dir=data_dir).events(run_id)

    def cli(self, argv: Sequence[str] | None = None) -> int:
        from .cli import run_flow_cli

        return run_flow_cli(self, argv=argv)

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
