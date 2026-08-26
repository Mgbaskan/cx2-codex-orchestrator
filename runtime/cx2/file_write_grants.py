from __future__ import annotations

from pathlib import Path
import threading
import os
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
            if _destructive_change(item):
                return True
    elif isinstance(value, list):
        return any(_destructive_change(item) for item in value)
    return False


def _forbidden_mutation_text(value: Any) -> bool:
    """Reject command-like or privileged text hidden in nested change data."""
    forbidden_text = (
        "git reset --hard",
        "git clean",
        "reset --hard",
        "host execution",
        "host_execution",
        "dangerfullaccess",
        "additionalpermissions",
    )
    if isinstance(value, dict):
        for key, item in value.items():
            key_folded = str(key).casefold()
            if any(token in key_folded for token in forbidden_text):
                return True
            if key_folded in {
                "shell",
                "hostexecution",
                "host_execution",
                "dangerfullaccess",
                "additionalpermissions",
            }:
                return True
            if "command" in key_folded and item is not None and item is not False and item != "":
                return True
            if key_folded in {"action", "operation", "kind", "type"} and str(item).casefold() in {
                "shell",
                "execute",
                "execution",
                "host",
            }:
                return True
            if _forbidden_mutation_text(item):
                return True
    elif isinstance(value, list):
        return any(_forbidden_mutation_text(item) for item in value)
    elif isinstance(value, str):
        folded = value.casefold()
        return any(token in folded for token in forbidden_text)
    return False


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
        lowered = raw.casefold()
        if any(token in lowered for token in ("delete", "remove", "unlink", "rename")):
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
