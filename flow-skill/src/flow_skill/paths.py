"""Workspace path contract helpers (see docs/文件路径契约.md)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .errors import FlowDefinitionError, ValidationError

__all__ = [
    "normalize_workspace_path",
    "resolve_workspace_path",
    "is_within_workspace",
]


def normalize_workspace_path(path: str, *, definition: bool = False) -> PurePosixPath:
    """Validate and normalize a workspace-relative contract path.

    Rules (文件路径契约 §3.1):
    - must be relative (no leading drive/slash);
    - must not contain ``..`` segments;
    - backslashes are normalized to forward slashes;
    - empty paths are invalid.
    """
    error_cls = FlowDefinitionError if definition else ValidationError
    if not isinstance(path, str) or not path.strip():
        raise error_cls("路径不能为空：必须是不含 .. 的 workspace 相对路径")
    unified = path.replace("\\", "/").strip()
    pure = PurePosixPath(unified)
    if pure.is_absolute() or (len(unified) > 1 and unified[1] == ":") or ".." in pure.parts:
        raise error_cls(
            f"路径 '{path}' 非法：必须是不含 .. 的 workspace 相对路径"
            + ("（定义期检查）" if definition else "")
        )
    if "." in pure.parts:
        raise error_cls(f"路径 '{path}' 非法：不允许出现 '.' 路径段")
    return pure


def resolve_workspace_path(workspace: Path, path: str) -> Path:
    """Resolve a contract path against the workspace root (no existence check)."""
    pure = normalize_workspace_path(path)
    return workspace.joinpath(*pure.parts)


def is_within_workspace(workspace: Path, path: Path | str) -> bool:
    """Strictly check that *path* resolves inside *workspace* (symlink aware).

    Both sides are resolved through ``Path.resolve()`` so that ``..`` segments
    and symlinks pointing outside the workspace are rejected.
    """
    root = Path(workspace).resolve()
    candidate = Path(path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True
