from __future__ import annotations

from pathlib import Path
from pathlib import PureWindowsPath
import threading
import os
import re
from typing import Any


GRANT_KIND = "ordinary_workspace_file_mutation"
MAX_RUNTIME_GRANTS = 256


def canonical_workspace_root(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _path_values(value: Any) -> list[str]:
    """Extract explicit paths and legacy ``{path: change}`` map keys.

    Approval metadata keys must never be interpreted as filesystem paths.  A
    payload without an actual target therefore fails closed.
    """
    values: list[str] = []
    if isinstance(value, dict):
        path_fields = {
            "path", "file", "relativepath", "target", "filepath", "filename",
            "newpath", "destination",
        }
        metadata_fields = {
            "action", "operation", "kind", "type", "status", "reason",
            "content", "diff", "patch", "oldpath", "deleted", "created",
        }
        for key, item in value.items():
            folded = str(key).casefold()
            if folded in path_fields:
                if isinstance(item, str):
                    values.append(item)
                elif isinstance(item, list):
                    for path_item in item:
                        if isinstance(path_item, str):
                            values.append(path_item)
                continue
            if folded in {"paths", "files", "targets", "changes", "filechanges"}:
                values.extend(_path_values(item))
                continue
            # Legacy applyPatchApproval uses the target path as the map key.
            # Only structured change descriptors qualify; ordinary metadata
            # such as {"action": "edit"} is not a target.
            if folded not in metadata_fields and isinstance(item, dict):
                descriptor_keys = {str(field).casefold() for field in item}
                if descriptor_keys & {
                    "action", "operation", "kind", "type", "content", "diff",
                    "patch", "deleted", "created",
                }:
                    values.append(str(key))
            if isinstance(item, (dict, list)):
                values.extend(_path_values(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(_path_values(item))
    elif isinstance(value, str):
        values.append(value)
    return values


def _destructive_change(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            key_folded = str(key).casefold()
            if key_folded in {"delete", "deleted", "remove", "removed", "unlink", "rename"}:
                if item is True or (
                    not isinstance(item, (dict, list))
                    and item not in {False, None, "", "edit", "create", "add", "modify", "patch"}
                ):
                    return True
            if key_folded in {"action", "operation", "kind", "type"} and str(item).casefold() in {
                "delete", "deleted", "remove", "removed", "unlink", "rename"
            }:
                return True
            if key_folded not in {
                "content", "diff", "patch", "reason", "message", "description",
            } and _destructive_change(item):
                return True
    elif isinstance(value, list):
        return any(_destructive_change(item) for item in value)
    return False


def _forbidden_mutation_text(value: Any) -> bool:
    """Reject structured privilege requests, never ordinary prose/content."""
    if isinstance(value, dict):
        for key, item in value.items():
            key_folded = str(key).casefold()
            if key_folded in {
                "shell",
                "hostexecution",
                "host_execution",
                "dangerfullaccess",
                "additionalpermissions",
            }:
                if item is not None and item is not False and item != "":
                    return True
            if key_folded in {"command", "commands", "exec", "execution"} and item:
                return True
            if key_folded in {"action", "operation", "kind", "type"} and str(item).casefold() in {
                "shell",
                "execute",
                "execution",
                "host",
            }:
                return True
            if key_folded not in {
                "content", "diff", "patch", "reason", "message", "description",
            } and _forbidden_mutation_text(item):
                return True
    elif isinstance(value, list):
        return any(_forbidden_mutation_text(item) for item in value)
    return False


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", "CLOCK$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _unambiguous_windows_path(raw: str) -> bool:
    """Reject Win32 aliases and namespaces before filesystem resolution."""

    if not raw or any(ord(char) < 0x20 for char in raw):
        return False
    normalized = raw.replace("/", "\\")
    if normalized.startswith(("\\\\?\\", "\\\\.\\")):
        return False
    if re.match(r"^[A-Za-z]:(?!\\)", normalized):
        return False
    drive_absolute = bool(re.match(r"^[A-Za-z]:\\", normalized))
    colon_source = normalized[2:] if drive_absolute else normalized
    if ":" in colon_source:
        return False
    if any(char in normalized for char in '<>"|?*'):
        return False
    for part in PureWindowsPath(normalized).parts:
        if part in {"\\", "/"} or re.fullmatch(r"[A-Za-z]:\\", part):
            continue
        if part.endswith((".", " ")):
            return False
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            return False
    return True


def ordinary_workspace_file_mutation(
    params: dict[str, Any],
    *,
    workspace_root: Path,
) -> bool:
    """Accept only non-destructive file changes wholly inside the workspace."""
    root = workspace_root.resolve()
    if params.get("additionalPermissions") is not None:
        return False
    for forbidden in ("dangerFullAccess", "danger_full_access", "hostExecution", "shell", "privileged"):
        if params.get(forbidden):
            return False
    requested_root = params.get("grantRoot")
    if requested_root:
        try:
            if Path(str(requested_root)).resolve() != root:
                return False
        except (OSError, ValueError):
            return False
    changes = params.get("fileChanges")
    if changes is None:
        changes = params.get("changes")
    if _destructive_change(changes) or _forbidden_mutation_text(params):
        return False
    paths = _path_values(changes)
    if not paths:
        return False
    for raw in paths:
        if not _unambiguous_windows_path(raw):
            return False
        try:
            path = Path(raw)
            resolved = (path if path.is_absolute() else root / path).resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            return False
    return True


class FileWriteGrantRegistry:
    """Runtime-scoped grants; no disk persistence and no cross-workspace reuse."""

    def __init__(self, runtime_instance_nonce: str) -> None:
        self.runtime_instance_nonce = str(runtime_instance_nonce)
        self._grants: dict[tuple[str, str, str, str], None] = {}
        self._lock = threading.RLock()

    def key(self, *, thread_id: str, workspace_root: Path) -> tuple[str, str, str, str]:
        return (
            self.runtime_instance_nonce,
            str(thread_id),
            canonical_workspace_root(workspace_root),
            GRANT_KIND,
        )

    def has(self, *, thread_id: str, workspace_root: Path) -> bool:
        with self._lock:
            return self.key(thread_id=thread_id, workspace_root=workspace_root) in self._grants

    def grant(self, *, thread_id: str, workspace_root: Path) -> None:
        with self._lock:
            key = self.key(thread_id=thread_id, workspace_root=workspace_root)
            if key not in self._grants and len(self._grants) >= MAX_RUNTIME_GRANTS:
                self._grants.pop(next(iter(self._grants)))
            self._grants[key] = None

    def clear(self) -> None:
        with self._lock:
            self._grants.clear()


__all__ = [
    "FileWriteGrantRegistry",
    "GRANT_KIND",
    "MAX_RUNTIME_GRANTS",
    "canonical_workspace_root",
    "ordinary_workspace_file_mutation",
]
