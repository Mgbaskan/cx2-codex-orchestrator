from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    # Keep all disposable interpreter/tool caches outside the repository. The
    # process environment is local to this test run and never mutates user policy.
    with tempfile.TemporaryDirectory(prefix="cx2-isolated-tests-") as temp_dir:
        temp_root = Path(temp_dir)
        env = os.environ.copy()
        paths = {
            "TEMP": temp_root / "temp",
            "TMP": temp_root / "temp",
            "TMPDIR": temp_root / "temp",
            "PYTHONPYCACHEPREFIX": temp_root / "pycache",
            "GOCACHE": temp_root / "go-build",
            "GOTMPDIR": temp_root / "go-tmp",
            "NPM_CONFIG_CACHE": temp_root / "npm-cache",
            "USERPROFILE": temp_root / "user-home",
            "HOME": temp_root / "user-home",
        }
        for path in set(paths.values()):
            path.mkdir(parents=True, exist_ok=True)
        for name, path in paths.items():
            env[name] = str(path)

        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "tests"],
            cwd=ROOT,
            env=env,
            check=False,
        )
        return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
