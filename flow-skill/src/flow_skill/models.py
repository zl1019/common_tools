"""State and response models used by Flow-Skill."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .errors import FlowDefinitionError
from .paths import normalize_workspace_path
from .schema import FILE_FORMATS, assert_schema_subset


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
        return {key: value for key, value in asdict_items(self).items() if value is not None}


@dataclass(slots=True)
class SubmissionResponse:
    status: str
    run_id: str
    task_id: str
    errors: list[str] = field(default_factory=list)
    retry_count: int | None = None
    next_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict_items(self).items() if value is not None}


def asdict_items(obj: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(obj)


# ---------------------------------------------------------------------------
# Input / Output contract specs (输入输出契约设计 §2-§4)
# ---------------------------------------------------------------------------

INPUT_SOURCES = ("run_inputs", "other_task", "static", "review")

_TYPE_NAMES = {str: "string", int: "integer", float: "number", bool: "boolean",
               list: "array", dict: "object"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise FlowDefinitionError(message)


@dataclass(slots=True)
class InputSpec:
    """Normalized input contract; the three public constructors are sugar."""

    kind: str = ""
    name: str = ""
    source: str | None = None
    from_task: str | None = None
    output: str | None = None
    key: str | None = None
    required: bool = True
    value: str | None = None
    format: tuple[str, ...] = ()
    sheet: str | None = None
    sheets: tuple[str, ...] = ()
    schema: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "kind": self.kind,
            "name": self.name,
            "source": self.source,
            "from_task": self.from_task,
            "output": self.output,
            "key": self.key,
            "required": self.required,
            "value": self.value,
            "format": list(self.format),
            "sheet": self.sheet,
            "sheets": list(self.sheets),
            "schema": self.schema,
        }
        return {key: value for key, value in data.items() if value is not None}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "InputSpec":
        payload = dict(data)
        kind = payload.pop("kind")
        spec_class = {"text": TextInput, "json": JsonInput, "file": FileInput}[kind]
        for list_field in ("format", "sheets"):
            if isinstance(payload.get(list_field), list):
                payload[list_field] = tuple(payload[list_field])
        return spec_class(**payload)  # type: ignore[abstract]


@dataclass(slots=True)
class OutputSpec:
    """Normalized output contract; the two public constructors are sugar."""

    kind: str = ""
    name: str = ""
    schema: dict[str, Any] | None = None
    path: str | None = None
    format: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "kind": self.kind,
            "name": self.name,
            "schema": self.schema,
            "path": self.path,
            "format": self.format,
            "metadata": self.metadata,
        }
        return {key: value for key, value in data.items() if value is not None}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "OutputSpec":
        payload = dict(data)
        kind = payload.pop("kind")
        spec_class = {"json": JsonOutput, "file": FileOutput}[kind]
        return spec_class(**payload)  # type: ignore[abstract]


@dataclass(slots=True)
class TextInput(InputSpec):
    """Static/short-text input; never resolved as a path."""

    kind: str = "text"

    def __post_init__(self) -> None:
        _require(bool(self.name), "TextInput.name cannot be empty")
        _require(
            self.source in (None, "run_inputs", "static"),
            f"TextInput '{self.name}': source 必须是 run_inputs 或静态 value",
        )
        _require(
            not (self.value is not None and self.source not in (None, "static")),
            f"TextInput '{self.name}': value 与 source 互斥",
        )
        if self.source is None and self.value is None:
            raise FlowDefinitionError(
                f"TextInput '{self.name}': 必须提供 value 或 source='run_inputs'"
            )
        if self.value is not None:
            _require(isinstance(self.value, str), f"TextInput '{self.name}': value 必须是字符串")
        _require(self.from_task is None and self.output is None,
                 f"TextInput '{self.name}': 不支持 from_task/output")
        _require(self.format == () and self.sheet is None and self.sheets == (),
                 f"TextInput '{self.name}': 不支持 format/sheet/sheets")
        _require(self.schema is None, f"TextInput '{self.name}': 不支持 schema")
        if self.source == "static":
            self.source = None


@dataclass(slots=True)
class JsonInput(InputSpec):
    """Structured JSON input; strings are rejected to avoid path ambiguity."""

    kind: str = "json"

    def __post_init__(self) -> None:
        _require(bool(self.name), "JsonInput.name cannot be empty")
        if self.source is None and self.from_task is not None:
            self.source = "other_task"  # from_task 隐含 source
        _require(
            self.source in ("run_inputs", "other_task", "review"),
            f"JsonInput '{self.name}': source 必须是 run_inputs / other_task / review",
        )
        if self.source == "other_task":
            _require(bool(self.from_task),
                     f"JsonInput '{self.name}': source='other_task' 时必须提供 from_task")
        else:
            _require(self.from_task is None,
                     f"JsonInput '{self.name}': 仅 source='other_task' 支持 from_task")
        _require(self.value is None, f"JsonInput '{self.name}': 不支持 value")
        _require(self.format == () and self.sheet is None and self.sheets == (),
                 f"JsonInput '{self.name}': 不支持 format/sheet/sheets（文件一律走 FileInput）")
        if self.schema is not None:
            assert_schema_subset(self.schema)


@dataclass(slots=True)
class FileInput(InputSpec):
    """File input from an upstream task; resolved to an absolute workspace path."""

    kind: str = "file"

    def __post_init__(self) -> None:
        _require(bool(self.name), "FileInput.name cannot be empty")
        _require(self.source in (None, "other_task"),
                 f"FileInput '{self.name}': source 必须是 other_task")
        _require(bool(self.from_task),
                 f"FileInput '{self.name}': 必须提供 from_task（文件必然来自上游任务）")
        self.source = "other_task"
        _require(not (self.sheet is not None and self.sheets),
                 f"FileInput '{self.name}': sheet 与 sheets 互斥")
        _require(self.value is None and self.key is None,
                 f"FileInput '{self.name}': 不支持 value/key")
        _require(self.schema is None, f"FileInput '{self.name}': 不支持 schema")
        invalid = [item for item in self.format if item not in FILE_FORMATS]
        if invalid:
            raise FlowDefinitionError(
                f"FileInput '{self.name}': 不支持的文件格式 {invalid}（允许: {sorted(FILE_FORMATS)}）"
            )


class JsonOutput(OutputSpec):
    """Structured JSON output with optional schema contract."""

    def __init__(
        self,
        *,
        name: str,
        schema: dict[str, Any] | None = None,
        required: Mapping[str, type] | None = None,
        optional: Mapping[str, type] | None = None,
    ) -> None:
        _require(bool(name), "JsonOutput.name cannot be empty")
        _require(schema is None or (required is None and optional is None),
                 "JsonOutput: schema 与 required/optional 互斥")
        built_schema = schema
        if built_schema is None and (required or optional):
            built_schema = _simple_schema(required, optional)
        if built_schema is not None:
            assert_schema_subset(built_schema)
        super().__init__(kind="json", name=name, schema=built_schema)


class FileOutput(OutputSpec):
    """File output contract: required name/path/format, metadata per format."""

    def __init__(
        self,
        *,
        name: str,
        path: str,
        format: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        _require(bool(name), "FileOutput.name cannot be empty")
        normalized = normalize_workspace_path(path, definition=True)
        _require(bool(format) and format.lower() in FILE_FORMATS,
                 f"FileOutput '{name}': 不支持的格式 '{format}'（允许: {sorted(FILE_FORMATS)}）")
        metadata_dict = dict(metadata or {})
        _validate_file_metadata(name, format.lower(), metadata_dict)
        super().__init__(
            kind="file",
            name=name,
            path=str(normalized),
            format=format.lower(),
            metadata=metadata_dict or None,
        )


def _simple_schema(
    required: Mapping[str, type] | None,
    optional: Mapping[str, type] | None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for field_map in (required, optional):
        for key, python_type in (field_map or {}).items():
            if python_type not in _TYPE_NAMES:
                raise FlowDefinitionError(f"不支持的类型标注: {python_type}")
            properties[key] = {"type": _TYPE_NAMES[python_type]}
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


def _validate_file_metadata(name: str, file_format: str, metadata: Mapping[str, Any]) -> None:
    allowed: dict[str, set[str]] = {
        "xlsx": {"sheets"},
        "md": {"headings", "min_chars"},
        "html": {"contains", "min_chars"},
    }
    permitted = allowed.get(file_format, set())
    unknown = sorted(set(metadata) - permitted)
    if unknown:
        suffix = "" if permitted else f"（格式 '{file_format}' 不支持 metadata）"
        raise FlowDefinitionError(
            f"FileOutput '{name}': metadata 键 {unknown} 与 format='{file_format}' 不匹配{suffix}"
        )
    if file_format == "xlsx":
        for item in metadata.get("sheets", []) or []:
            if isinstance(item, Mapping):
                _require("name" in item, f"FileOutput '{name}': sheets 项必须包含 name")
                extra = sorted(set(item) - {"name", "min_rows"})
                _require(not extra, f"FileOutput '{name}': sheets 项不支持键 {extra}")
            else:
                _require(isinstance(item, str),
                         f"FileOutput '{name}': sheets 项必须是名称或对象")
    if file_format in {"md", "html"}:
        for key in ("headings", "contains"):
            if key in metadata:
                _require(
                    isinstance(metadata[key], list)
                    and all(isinstance(item, str) for item in metadata[key]),
                    f"FileOutput '{name}': {key} 必须是字符串列表",
                )
        if "min_chars" in metadata:
            _require(
                isinstance(metadata["min_chars"], int) and metadata["min_chars"] >= 0,
                f"FileOutput '{name}': min_chars 必须是非负整数",
            )
