from __future__ import annotations

import atexit
import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import shutil
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
_temp_parent = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "Temp"
_temp_parent.mkdir(parents=True, exist_ok=True)
TEST_USER_HOME = Path(
    tempfile.mkdtemp(prefix="cx2-test-home-", dir=_temp_parent)
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
