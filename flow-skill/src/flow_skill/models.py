"""State and response models used by Flow-Skill."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class TaskType(StringEnum):
    AGENT = "agent"
    HUMAN = "human"


class TaskStatus(StringEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_RETRY_CONFIRMATION = "waiting_retry_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(StringEnum):
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


class ResultType(StringEnum):
    EXECUTION_RESULT = "execution_result"
    EXECUTION_ERROR = "execution_error"
    USER_DECISION = "user_decision"
    USER_INPUT = "user_input"


class ResponseStatus(StringEnum):
    TASK_READY = "task_ready"
    WAITING_USER = "waiting_user"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


class SubmissionStatus(StringEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RETRY_SCHEDULED = "retry_scheduled"
    WAITING = "waiting"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


@dataclass(slots=True)
class ValidationResult:
    accepted: bool
    errors: list[str] = field(default_factory=list)

    @classmethod
    def accept(cls) -> "ValidationResult":
        return cls(accepted=True)

    @classmethod
    def reject(cls, *errors: str) -> "ValidationResult":
        return cls(accepted=False, errors=list(errors))


@dataclass(slots=True)
class FlowResponse:
    status: str
    run_id: str
    task: dict[str, Any] | None = None
    reason: str | None = None
    prompt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(slots=True)
class SubmissionResponse:
    status: str
    run_id: str
    task_id: str
    errors: list[str] = field(default_factory=list)
    retry_count: int | None = None
    next_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}

