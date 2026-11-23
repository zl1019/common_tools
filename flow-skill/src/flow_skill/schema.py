"""Zero-dependency contract verifiers.

Two responsibilities (输入输出契约设计 §6 + 文件路径契约 §3):

1. JSON Schema built-in subset validator (no jsonschema dependency);
2. File contract verification per format (xlsx sheet list via zipfile,
   md headings, html fragments, generic existence checks).
"""

from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import FlowDefinitionError

__all__ = [
    "assert_schema_subset",
    "schema_errors",
    "xlsx_sheet_names",
    "verify_output_contract",
    "verify_file_metadata",
    "FILE_FORMATS",
]

FILE_FORMATS = {"xlsx", "csv", "md", "html", "pdf", "json", "txt"}

_SCALAR_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}

_ALLOWED_KEYWORDS = {
    "type",
    "properties",
    "required",
    "items",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "additionalProperties",
}


def assert_schema_subset(schema: Mapping[str, Any]) -> None:
    """Reject schemas using keywords outside the built-in subset (definition time)."""
    if not isinstance(schema, Mapping):
        raise FlowDefinitionError("JSON schema must be an object")
    unsupported = sorted(set(schema) - _ALLOWED_KEYWORDS)
    if unsupported:
        raise FlowDefinitionError(
            "JSON schema uses unsupported keywords: "
            + ", ".join(unsupported)
            + " (built-in subset supports: "
            + ", ".join(sorted(_ALLOWED_KEYWORDS))
            + ")"
        )
    for key in ("properties", "items"):
        if key in schema:
            if key == "properties":
                if not isinstance(schema[key], Mapping):
                    raise FlowDefinitionError("schema 'properties' must be an object")
                for child in schema[key].values():
                    assert_schema_subset(child)
            else:
                assert_schema_subset(schema["items"])


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected in _SCALAR_TYPES:
        return isinstance(value, _SCALAR_TYPES[expected])
    return False


def schema_errors(value: Any, schema: Mapping[str, Any], *, path: str = "") -> list[str]:
    """Validate *value* against the built-in schema subset; return error list."""
    errors: list[str] = []
    label = path or "value"
    expected_types = schema.get("type")
    if expected_types is not None:
        types = [expected_types] if isinstance(expected_types, str) else list(expected_types)
        if not any(_type_matches(value, item) for item in types):
            return [f"{label} 类型不符（应为 {'/'.join(types)}）"]

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{label} 不在允许的枚举值内: {schema['enum']!r}")
        return errors

    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{label}.{name} 缺失（required）")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                errors.append(f"{label} 含未声明的字段: {', '.join(extras)}")
        for name, child_schema in properties.items():
            if name in value:
                errors.extend(schema_errors(value[name], child_schema, path=f"{label}.{name}"))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{label} 元素数少于 minItems={schema['minItems']}")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                errors.extend(schema_errors(item, items, path=f"{label}[{index}]"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{label} 长度少于 minLength={schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{label} 长度超过 maxLength={schema['maxLength']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{label} 小于 minimum={schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{label} 大于 maximum={schema['maximum']}")

    return errors


# ---------------------------------------------------------------------------
# File contract verification
# ---------------------------------------------------------------------------


def xlsx_sheet_names(path: Path) -> list[str]:
    """Read sheet names from an xlsx (zip) via xl/workbook.xml - stdlib only."""
    with zipfile.ZipFile(path) as archive:
        try:
            raw = archive.read("xl/workbook.xml")
        except KeyError as exc:
            raise ValueError("文件不是有效的 xlsx（缺少 xl/workbook.xml）") from exc
    root = ET.fromstring(raw)
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return [str(node.get("name")) for node in root.findall("main:sheets/main:sheet", namespace)]


def _sheet_rows_hint(path: Path) -> dict[str, int]:  # pragma: no cover - reserved
    """Best-effort row count per sheet via sheet dimension refs; reserved."""
    return {}


def verify_file_metadata(
    *,
    file_path: Path,
    task_id: str,
    metadata: Mapping[str, Any] | None,
    declared_tasks_for_path: Iterable[str],
) -> list[str]:
    """Verify format-specific metadata for a submitted file output.

    - xlsx: declared sheets exist; sheet names must carry a declared task
      prefix (``<task_id>__``); orphan sheets (not owned by any task that
      declares this workbook) are rejected.
    - md: headings appear in order; min_chars lower bound.
    - html: fragments contained; min_chars lower bound.
    """
    errors: list[str] = []
    metadata = dict(metadata or {})
    if not file_path.exists():
        return [f"文件不存在: {file_path}"]

    if file_path.suffix.lower() == ".xlsx":
        try:
            actual_sheets = xlsx_sheet_names(file_path)
        except (zipfile.BadZipFile, ValueError, ET.ParseError) as exc:
            return [f"xlsx 解析失败: {exc}"]

        owned_prefixes = [f"{task}__" for task in declared_tasks_for_path]
        declared_names: list[str] = []
        for item in metadata.get("sheets", []) or []:
            name = item["name"] if isinstance(item, Mapping) else str(item)
            declared_names.append(name)
            if not name.startswith(f"{task_id}__"):
                errors.append(f"sheet '{name}' 不属于任务 '{task_id}'（缺少前缀 {task_id}__）")
            if name not in actual_sheets:
                errors.append(f"缺少声明 sheet: {name}")
        if owned_prefixes:
            for name in actual_sheets:
                if not any(name.startswith(prefix) for prefix in owned_prefixes):
                    errors.append(f"孤儿 sheet: '{name}'（不属于任何声明该工作簿的任务）")

    elif file_path.suffix.lower() in {".md", ".markdown"}:
        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return [f"md 读取失败: {exc}"]
        headings = metadata.get("headings") or []
        if headings:
            lines = [line.strip() for line in text.splitlines()]
            cursor = 0
            for heading in headings:
                found = None
                for index in range(cursor, len(lines)):
                    if lines[index] == str(heading):
                        found = index
                        break
                if found is None:
                    errors.append(f"缺少声明标题或标题乱序: {heading}")
                else:
                    cursor = found + 1
        min_chars = metadata.get("min_chars")
        if min_chars is not None and len(text) < int(min_chars):
            errors.append(f"篇幅低于 min_chars={min_chars}（实际 {len(text)}）")

    elif file_path.suffix.lower() in {".html", ".htm"}:
        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return [f"html 读取失败: {exc}"]
        for fragment in metadata.get("contains") or []:
            if str(fragment) not in text:
                errors.append(f"缺少声明片段: {fragment}")
        min_chars = metadata.get("min_chars")
        if min_chars is not None and len(text) < int(min_chars):
            errors.append(f"篇幅低于 min_chars={min_chars}（实际 {len(text)}）")

    else:
        if metadata:
            errors.append(f"格式 '{file_path.suffix}' 不支持 metadata 契约参数")

    return errors


def verify_output_contract(
    *,
    output_specs: Sequence[Any],
    result: Any,
    workspace: Path,
    task_id: str,
    declared_tasks_for_path: Iterable[str],
) -> list[str]:
    """Contract verification (提交期 ② 层) for tasks declaring outputs.

    Returns a list of error strings; empty list means the contract holds.
    """
    errors: list[str] = []
    if not isinstance(result, Mapping):
        return ["result 必须是 JSON 对象（任务声明了 outputs 契约）"]

    for spec in output_specs:
        name = spec.name
        if name not in result:
            errors.append(f"缺少声明输出 '{name}'")
            continue
        value = result[name]

        if spec.kind == "json":
            if isinstance(value, str):
                errors.append(
                    f"{name} 应为结构化 JSON，实为字符串（文件路径请用 FileOutput）"
                )
                continue
            if not isinstance(value, (dict, list, int, float, bool)) and value is not None:
                errors.append(f"{name} 应为结构化 JSON，实为 {type(value).__name__}")
                continue
            if spec.schema is not None and value is not None:
                errors.extend(schema_errors(value, spec.schema, path=name))

        elif spec.kind == "file":
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{name} 应为文件路径字符串")
                continue
            from .paths import normalize_workspace_path, resolve_workspace_path

            try:
                submitted = normalize_workspace_path(value)
                declared = normalize_workspace_path(spec.path or "")
            except Exception as exc:  # noqa: BLE001 - surface as contract error
                errors.append(f"{name} 路径非法: {exc}")
                continue
            if declared.parts and submitted != declared:
                errors.append(
                    f"{name} 路径与声明不一致: 提交 '{value}'，声明 '{spec.path}'"
                )
                file_path = resolve_workspace_path(workspace, spec.path or value)
            else:
                file_path = resolve_workspace_path(workspace, value)
            if not file_path.exists():
                errors.append(f"文件不存在: {value}")
                continue
            expected_suffix = f".{(spec.format or '').lower()}"
            if file_path.suffix.lower() != expected_suffix:
                errors.append(f"{name} 扩展名不符: 期望 {expected_suffix}，实际 {file_path.suffix}")
                continue
            errors.extend(
                verify_file_metadata(
                    file_path=file_path,
                    task_id=task_id,
                    metadata=spec.metadata,
                    declared_tasks_for_path=declared_tasks_for_path,
                )
            )

    return errors
