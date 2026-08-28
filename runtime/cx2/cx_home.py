"""
CX_HOME Authoritative Resolver.

Provides deterministic resolution of the CX installation root and persistent state directory.

Resolution Contract:
1. Installed Runtime:
   If the executing Python interpreter (`sys.executable`) resides within `<root>/runtime/venv`
   AND the resolver module resides within `<root>/runtime/cx2`, CX_HOME is bound strictly
   to `<root>`.
   This ensures an installed runtime (via native cx.exe or fallback cx.cmd) is completely
   isolated to its installation directory and never leaks to or depends on %USERPROFILE%\\.cx.
2. Default Development / User Home Fallback:
   Otherwise (e.g. running repository tests or tools with repo .venv), defaults to
   `Path.home() / ".cx"`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def _is_subpath(child: Path, parent: Path) -> bool:
    """
    Check if `child` is equal to or strictly inside `parent`.
    Boundary-safe: 'C:\\foo\\runtime\\venv-other' is NOT inside 'C:\\foo\\runtime\\venv'.
    Handles Windows case-insensitivity, trailing slashes, and path normalization.
    """
    try:
        norm_child = Path(os.path.normcase(str(child.resolve())))
        norm_parent = Path(os.path.normcase(str(parent.resolve())))
        norm_child.relative_to(norm_parent)
        return True
    except (ValueError, TypeError, Exception):
        return False


def is_installed_runtime(
    module_file: Optional[str] = None,
    executable: Optional[str] = None,
) -> bool:
    """
    Determines whether the current process is executing from an installed CX runtime.

    Evidence requires BOTH:
    1. The resolver module file resides in `<root>/runtime/cx2` (exact directory 'cx2' under 'runtime')
    2. The executing Python interpreter (`sys.executable`) resides within `<root>/runtime/venv`
    """
    try:
        mod_path = Path(module_file if module_file else __file__).resolve()
        cx2_dir = mod_path.parent
        runtime_dir = cx2_dir.parent
        root_dir = runtime_dir.parent

        if os.path.normcase(cx2_dir.name) != "cx2" or os.path.normcase(runtime_dir.name) != "runtime":
            return False

        venv_dir = runtime_dir / "venv"
        exe_path = Path(executable if executable else sys.executable).resolve()

        return _is_subpath(exe_path, venv_dir)
    except Exception:
        return False


def resolve_cx_home(
    module_file: Optional[str] = None,
    executable: Optional[str] = None,
) -> Path:
    """
    Authoritatively resolve CX_HOME according to the contract:
    1. Installed runtime identity: <root> where interpreter is in <root>/runtime/venv
       and module is in <root>/runtime/cx2.
    2. Default fallback: Path.home() / ".cx".
    """
    disposable_canary_home = os.environ.get("CX2_DISPOSABLE_CANARY_HOME")
    if disposable_canary_home:
        if os.environ.get("CX2_CANARY_MODE") != "1":
            raise RuntimeError(
                "CX2_DISPOSABLE_CANARY_HOME requires explicit CX2_CANARY_MODE=1"
            )
        candidate = Path(disposable_canary_home)
        if not candidate.is_absolute():
            raise RuntimeError("Disposable canary CX_HOME must be absolute")
        return candidate.resolve()

    try:
        mod_path = Path(module_file if module_file else __file__).resolve()
        cx2_dir = mod_path.parent
        runtime_dir = cx2_dir.parent
        root_dir = runtime_dir.parent

        if os.path.normcase(cx2_dir.name) == "cx2" and os.path.normcase(runtime_dir.name) == "runtime":
            venv_dir = runtime_dir / "venv"
            exe_path = Path(executable if executable else sys.executable).resolve()
            if _is_subpath(exe_path, venv_dir):
                return root_dir.resolve()
    except Exception:
        pass

    try:
        return (Path.home() / ".cx").resolve()
    except Exception:
        return Path(".cx").resolve()
