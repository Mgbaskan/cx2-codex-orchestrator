from __future__ import annotations

import importlib.abc
import importlib.machinery
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
RUNTIME_DIR = REPO_ROOT / "runtime" / "cx2"
TESTS_DIR = REPO_ROOT / "tests"


class _RepoMetaPathFinder(importlib.abc.MetaPathFinder):
    """Ensures repository modules are always resolved from REPO_ROOT during test execution,
    preventing any fallback or insertion of ~/.cx production paths.
    """

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if "." in fullname:
            return None
        for dir_path in (SRC_DIR, RUNTIME_DIR):
            file_path = dir_path / f"{fullname}.py"
            if file_path.is_file():
                return importlib.machinery.PathFinder.find_spec(
                    fullname, [str(dir_path)], target
                )
        return None


def init_test_environment() -> Path:
    if not any(isinstance(f, _RepoMetaPathFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _RepoMetaPathFinder())

    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    if str(RUNTIME_DIR) not in sys.path:
        sys.path.insert(0, str(RUNTIME_DIR))
    if str(TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(TESTS_DIR))

    return REPO_ROOT


init_test_environment()
