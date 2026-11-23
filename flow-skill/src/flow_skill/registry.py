"""Per-skill run registry: run_id -> data_dir mapping (docs/文件路径契约.md §5).

The registry lives next to workflow.py (``.flow-registry.json``) or wherever
``FLOW_SKILL_REGISTRY`` points.  Writes are atomic; write failures degrade the
registry to "unavailable" mode (behaviour falls back to 0.1.x explicit
``--data-dir`` usage) instead of silently relocating data.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError

__all__ = [
    "ENV_DATA_DIR",
    "ENV_REGISTRY",
    "REGISTRY_FILENAME",
    "Registry",
    "resolve_create_data_dir",
    "resolve_resume_data_dir",
]

ENV_DATA_DIR = "FLOW_SKILL_DATA_DIR"
ENV_REGISTRY = "FLOW_SKILL_REGISTRY"
REGISTRY_FILENAME = ".flow-registry.json"

_REGISTRY_VERSION = 1


class Registry:
    """A single skill's run registry (one file per skill, naturally isolated)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.degraded_reason: str | None = None
        if path is not None:
            parent = path.parent
            if not parent.exists():
                self.degraded_reason = f"注册表目录不存在: {parent}"

    # -- construction -------------------------------------------------------

    @classmethod
    def for_flow(cls, flow: Any) -> "Registry":
        env_path = os.environ.get(ENV_REGISTRY)
        if env_path:
            return cls(Path(env_path))
        base_dir = getattr(flow, "base_dir", None)
        if base_dir is None:
            return cls(None)
        return cls(Path(base_dir) / REGISTRY_FILENAME)

    @property
    def available(self) -> bool:
        return self.path is not None and self.degraded_reason is None

    def status(self) -> dict[str, Any]:
        if self.path is None:
            return {"available": False,
                    "reason": "注册表不可用：未设置 base_dir 或 FLOW_SKILL_REGISTRY"}
        if self.degraded_reason is not None:
            return {"available": False, "reason": self.degraded_reason}
        return {"available": True, "path": str(self.path)}

    # -- io -----------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {"version": _REGISTRY_VERSION, "runs": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.degraded_reason = f"注册表读取失败: {exc}"
            return {"version": _REGISTRY_VERSION, "runs": {}}
        if not isinstance(data, Mapping) or not isinstance(data.get("runs"), Mapping):
            self.degraded_reason = "注册表文件结构无效，已忽略"
            return {"version": _REGISTRY_VERSION, "runs": {}}
        return {"version": _REGISTRY_VERSION, "runs": dict(data["runs"])}

    def _save(self, data: Mapping[str, Any]) -> bool:
        if self.path is None:
            self.degraded_reason = "注册表不可用：未设置 base_dir 或 FLOW_SKILL_REGISTRY"
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, delete=False, suffix=".tmp"
            )
            with handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(handle.name, self.path)
            return True
        except OSError as exc:
            self.degraded_reason = f"注册表写入失败（已降级为无注册表模式）: {exc}"
            return False

    # -- operations ---------------------------------------------------------

    def lookup(self, run_id: str) -> dict[str, Any] | None:
        return self._load()["runs"].get(run_id)

    def register(
        self,
        *,
        run_id: str,
        data_dir: Path,
        flow_name: str,
        flow_version: str,
        skill_path: str | None = None,
    ) -> None:
        data = self._load()
        runs = data["runs"]
        existing = runs.get(run_id)
        if existing is not None and Path(existing["data_dir"]) != data_dir:
            raise ValidationError(
                f"run_id '{run_id}' 已注册于 {existing['data_dir']}，禁止在别处重建同名 Run"
            )
        runs[run_id] = {
            "data_dir": str(data_dir),
            "flow_name": flow_name,
            "flow_version": flow_version,
            "skill_path": skill_path,
        }
        self._save(data)

    def rebind(self, *, run_id: str, data_dir: Path) -> None:
        data = self._load()
        if run_id not in data["runs"]:
            raise ValidationError(
                f"run_id '{run_id}' 不在注册表中，无法改绑（请先确认 Run 存在于 {data_dir}）"
            )
        data["runs"][run_id]["data_dir"] = str(data_dir)
        if not self._save(data):
            raise ValidationError(f"改绑失败：{self.degraded_reason}")


# ---------------------------------------------------------------------------
# data_dir resolution chain (文件路径契约 §5.1 / §5.3)
# ---------------------------------------------------------------------------


def resolve_create_data_dir(flow: Any, explicit: str | Path | None) -> Path:
    """create-run: --data-dir > FLOW_SKILL_DATA_DIR > Flow.default_data_dir > .flow-data."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return Path(env)
    default = getattr(flow, "default_data_dir", None)
    if default is not None:
        return Path(default)
    return Path(".flow-data")


def resolve_resume_data_dir(
    flow: Any,
    run_id: str,
    explicit: str | Path | None,
    *,
    rebind: bool = False,
) -> tuple[Path, Registry]:
    """Resume: the registry is the single source of truth.

    Returns (data_dir, registry).  Explicit paths that disagree with the
    registry raise unless ``rebind`` is set.
    """
    registry = Registry.for_flow(flow)
    record = registry.lookup(run_id) if registry.available else None
    if record is not None:
        registered_dir = Path(record["data_dir"])
        if explicit is not None:
            candidate = Path(explicit)
            if candidate != registered_dir:
                if not rebind:
                    raise ValidationError(
                        f"Run '{run_id}' 注册于 {registered_dir}，与 --data-dir {candidate} 不一致。"
                        f"若数据确已迁移，请加 --rebind 改绑"
                    )
                expected_db = candidate / getattr(flow, "name", "") / "workflow.db"
                if getattr(flow, "name", "") and not expected_db.exists():
                    raise ValidationError(
                        f"改绑目标 {candidate} 下不存在该 Run 的数据库（{expected_db}），改绑被拒绝"
                    )
                registry.rebind(run_id=run_id, data_dir=candidate)
                return candidate, registry
        return registered_dir, registry

    if explicit is not None:
        return Path(explicit), registry
    raise ValidationError(
        f"Run '{run_id}' 未在注册表中登记，且未提供 --data-dir。"
        f"请显式传 --data-dir（注册表状态: {registry.status()}）"
    )
