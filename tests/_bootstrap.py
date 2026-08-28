from __future__ import annotations

import atexit
import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
RUNTIME_DIR = REPO_ROOT / "runtime" / "cx2"
TESTS_DIR = REPO_ROOT / "tests"


# Development modules otherwise resolve Path.home()/.cx and can overwrite the
# installed runtime's mutable logs during tests. Give the entire test process a
# disposable external home before any repository runtime module is imported.
ORIGINAL_USER_HOME = Path(
    os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home()
).resolve()


def _absolute_path(path: Path) -> Path:
    """Normalize a path lexically without probing an unrelated filesystem target."""
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except AttributeError:
        return path.is_symlink()
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _validated_explicit_temp_root(raw_root: str) -> Path:
    root = _absolute_path(Path(raw_root))
    if not root.is_dir():
        raise RuntimeError(f"CX2_TEST_TEMP_ROOT is not an existing directory: {root}")
    if _is_reparse_point(root):
        raise RuntimeError(f"CX2_TEST_TEMP_ROOT must not be a reparse point: {root}")

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA is required with CX2_TEST_TEMP_ROOT on Windows")
        expected_parent = _absolute_path(Path(local_app_data) / "Temp")
        if root.parent != expected_parent:
            raise RuntimeError(
                "CX2_TEST_TEMP_ROOT must be an exact child of LocalAppData Temp: "
                f"{root}"
            )
        if not expected_parent.is_dir() or _is_reparse_point(expected_parent):
            raise RuntimeError(
                f"LocalAppData Temp must be a regular directory: {expected_parent}"
            )

    return root


_explicit_temp_root = os.environ.get("CX2_TEST_TEMP_ROOT")
if _explicit_temp_root:
    TEST_QUALIFICATION_ROOT = _validated_explicit_temp_root(_explicit_temp_root)
else:
    _local_app_data = os.environ.get("LOCALAPPDATA")
    TEST_QUALIFICATION_ROOT = (
        _absolute_path(Path(_local_app_data) / "Temp")
        if _local_app_data
        else Path(tempfile.gettempdir()).resolve()
    )
    TEST_QUALIFICATION_ROOT.mkdir(parents=True, exist_ok=True)

TEST_USER_HOME = Path(
    tempfile.mkdtemp(prefix="cx2-test-home-", dir=TEST_QUALIFICATION_ROOT)
).resolve()
os.environ["USERPROFILE"] = str(TEST_USER_HOME)
os.environ["HOME"] = str(TEST_USER_HOME)
TEST_TEMP_ROOT = TEST_USER_HOME
for _temp_name in ("TEMP", "TMP", "TMPDIR"):
    os.environ[_temp_name] = str(TEST_TEMP_ROOT)
# tempfile may have cached the inherited runner location before this bootstrap
# was imported, so environment overrides alone are not deterministic.
tempfile.tempdir = str(TEST_TEMP_ROOT)


@atexit.register
def _cleanup_test_user_home() -> None:
    shutil.rmtree(TEST_USER_HOME, ignore_errors=True)


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
