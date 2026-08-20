from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time


if len(sys.argv) < 7:
    raise SystemExit(
        "usage: safe_process_runner.py "
        "<cwd> <stdout> <stderr> <meta> "
        "<exe> <arg...>"
    )


cwd = Path(
    sys.argv[1]
).resolve()

stdout_path = Path(
    sys.argv[2]
).resolve()

stderr_path = Path(
    sys.argv[3]
).resolve()

meta_path = Path(
    sys.argv[4]
).resolve()

exe = sys.argv[5]

args = sys.argv[6:]


started = time.time()


completed = subprocess.run(
    [
        exe,
        *args,
    ],
    cwd=str(cwd),
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    encoding="utf-8",
    errors="replace",
    check=False,
)


elapsed_ms = int(
    (
        time.time()
        - started
    )
    * 1000
)


stdout_path.write_text(
    completed.stdout,
    encoding="utf-8",
)

stderr_path.write_text(
    completed.stderr,
    encoding="utf-8",
)


meta = {
    "exit_code":
        completed.returncode,

    "elapsed_ms":
        elapsed_ms,

    "stdout_chars":
        len(
            completed.stdout
        ),

    "stderr_chars":
        len(
            completed.stderr
        ),

    "cwd":
        str(
            cwd
        ),

    "exe":
        exe,

    "args":
        args,
}


meta_path.write_text(
    json.dumps(
        meta,
        ensure_ascii=False,
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)


print(
    json.dumps(
        meta,
        ensure_ascii=False,
    )
)


raise SystemExit(
    completed.returncode
)
