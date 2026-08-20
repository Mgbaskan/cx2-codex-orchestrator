from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any


CX_HOME = Path.home() / ".cx"
STAGE = CX_HOME / "runtime" / "cx2"
SRC = CX_HOME / "src"

for candidate in (
    str(STAGE),
    str(SRC),
):
    if candidate not in sys.path:
        sys.path.insert(
            0,
            candidate,
        )


import cx as production_cx
import cx2_cli


PYTHON = Path(sys.executable).resolve()

CLI = (
    STAGE
    / "cx2_cli.py"
)

ROLLOUT = Path(
    sys.argv[1]
).resolve()

REPO = Path(
    sys.argv[2]
).resolve()

RESULT = (
    STAGE
    / "cx2-cli-failure-boundary-last.json"
)

CRASH_LOG = (
    STAGE
    / "cx2-cli-last-crash.txt"
)


# =============================================================
# 1. Failed rollout exact tool boundary
# =============================================================

print(
    "=== FAILED TURN TOOL BOUNDARY ==="
)


tool_calls = []
tool_outputs = []
aborts = []
tokens = []


with ROLLOUT.open(
    "r",
    encoding="utf-8-sig",
    errors="replace",
) as handle:

    for line_number, line in enumerate(
        handle,
        start=1,
    ):

        try:
            obj = json.loads(
                line
            )
        except Exception:
            continue

        payload = obj.get(
            "payload"
        )

        if not isinstance(
            payload,
            dict,
        ):
            continue

        kind = payload.get(
            "type"
        )

        if kind == "custom_tool_call":

            tool_calls.append({
                "line":
                    line_number,

                "name":
                    payload.get(
                        "name"
                    ),

                "call_id":
                    payload.get(
                        "call_id"
                    ),

                "input":
                    payload.get(
                        "input"
                    ),
            })

        elif kind == "custom_tool_call_output":

            tool_outputs.append({
                "line":
                    line_number,

                "call_id":
                    payload.get(
                        "call_id"
                    ),

                "output":
                    payload.get(
                        "output"
                    ),
            })

        elif kind == "turn_aborted":

            aborts.append({
                "line":
                    line_number,

                "payload":
                    payload,
            })

        elif kind == "token_count":

            tokens.append({
                "line":
                    line_number,

                "payload":
                    payload,
            })


print(
    "Tool calls       :",
    len(tool_calls),
)

for item in tool_calls:

    print()
    print(
        "line            :",
        item[
            "line"
        ],
    )

    print(
        "name            :",
        item[
            "name"
        ],
    )

    print(
        "call_id         :",
        item[
            "call_id"
        ],
    )

    print(
        "input           :",
        repr(
            item[
                "input"
            ]
        )[:2000],
    )


print()
print(
    "Tool outputs     :",
    len(tool_outputs),
)

for item in tool_outputs:

    print()
    print(
        "line            :",
        item[
            "line"
        ],
    )

    print(
        "call_id         :",
        item[
            "call_id"
        ],
    )

    print(
        "output          :",
        repr(
            item[
                "output"
            ]
        )[:3000],
    )


print()
print(
    "Turn aborted     :",
    len(aborts),
)

print(
    "Token records    :",
    len(tokens),
)


# =============================================================
# 2. Separate-process CLI probes.
#
# --version / --route:
#     zero App Server
#
# --quota:
#     App Server initialize + quota only
#     ZERO model turn
# =============================================================

def run_process(
    args: list[str],
    *,
    cwd: Path,
) -> dict[str, Any]:

    completed = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )

    return {
        "returncode":
            completed.returncode,

        "stdout":
            completed.stdout,

        "stderr":
            completed.stderr,
    }


print()
print(
    "=== SUBPROCESS --version ==="
)

version_probe = run_process(
    [
        str(PYTHON),
        str(CLI),
        "--version",
    ],
    cwd=REPO,
)

print(
    "Exit   :",
    version_probe[
        "returncode"
    ],
)

print(
    "STDOUT :",
    repr(
        version_probe[
            "stdout"
        ]
    ),
)

print(
    "STDERR :",
    repr(
        version_probe[
            "stderr"
        ]
    ),
)

if version_probe[
    "returncode"
] != 0:

    raise RuntimeError(
        "--version subprocess failed."
    )


print()
print(
    "=== SUBPROCESS --route ==="
)

route_probe = run_process(
    [
        str(PYTHON),
        str(CLI),
        "--route",
        (
            "Bu repoda package.json dosyasi var mi? "
            "Sadece EVET veya HAYIR yaz."
        ),
    ],
    cwd=REPO,
)

print(
    "Exit   :",
    route_probe[
        "returncode"
    ],
)

print(
    "STDOUT :"
)

print(
    route_probe[
        "stdout"
    ]
)

print(
    "STDERR :",
    repr(
        route_probe[
            "stderr"
        ]
    ),
)

if route_probe[
    "returncode"
] != 0:

    raise RuntimeError(
        "--route subprocess failed."
    )


print()
print(
    "=== SUBPROCESS --quota ==="
)

quota_probe = run_process(
    [
        str(PYTHON),
        str(CLI),
        "--quota",
    ],
    cwd=REPO,
)

print(
    "Exit   :",
    quota_probe[
        "returncode"
    ],
)

print(
    "STDOUT :"
)

print(
    quota_probe[
        "stdout"
    ]
)

print(
    "STDERR :",
    repr(
        quota_probe[
            "stderr"
        ]
    ),
)

if quota_probe[
    "returncode"
] != 0:

    raise RuntimeError(
        "--quota subprocess failed."
    )


if CRASH_LOG.exists():

    crash_text = (
        CRASH_LOG.read_text(
            encoding="utf-8-sig",
            errors="replace",
        )
    )

else:
    crash_text = ""


print()
print(
    "Crash log after safe probes:",
    (
        "PRESENT"
        if crash_text.strip()
        else "NONE"
    ),
)


# =============================================================
# 3. Synthetic one-shot dispatch.
#
# This validates:
# parser -> prompt join -> DB open -> execute_one_shot dispatch
# without constructing App Server and without model inference.
# =============================================================

print()
print(
    "=== SYNTHETIC ONE-SHOT DISPATCH ==="
)


original_execute = (
    cx2_cli.execute_one_shot
)

original_open = (
    cx2_cli.open_usage_db
)


dispatch = {
    "called":
        False,

    "prompt":
        None,

    "cwd":
        None,

    "repo_root":
        None,
}


memory_db = sqlite3.connect(
    ":memory:"
)


def fake_open(
    override,
):

    return memory_db


def fake_execute(
    prompt: str,
    *,
    cwd: Path,
    repo: dict[str, Any],
    db: sqlite3.Connection,
) -> int:

    dispatch[
        "called"
    ] = True

    dispatch[
        "prompt"
    ] = prompt

    dispatch[
        "cwd"
    ] = str(
        cwd
    )

    dispatch[
        "repo_root"
    ] = str(
        repo[
            "root"
        ]
    )

    if db is not memory_db:
        raise RuntimeError(
            "Unexpected DB object."
        )

    return 0


cx2_cli.open_usage_db = (
    fake_open
)

cx2_cli.execute_one_shot = (
    fake_execute
)


old_cwd = Path.cwd()

try:

    os.chdir(
        REPO
    )

    with contextlib.redirect_stdout(
        io.StringIO()
    ):

        exit_code = cx2_cli.main([
            "synthetic",
            "one-shot",
            "prompt",
        ])

finally:

    os.chdir(
        old_cwd
    )

    cx2_cli.open_usage_db = (
        original_open
    )

    cx2_cli.execute_one_shot = (
        original_execute
    )


if exit_code != 0:

    raise RuntimeError(
        "Synthetic one-shot exit != 0."
    )


if not dispatch[
    "called"
]:

    raise RuntimeError(
        "execute_one_shot dispatch edilmedi."
    )


if dispatch[
    "prompt"
] != "synthetic one-shot prompt":

    raise RuntimeError(
        "Prompt join mismatch."
    )


print(
    "Parser -> one-shot : PASS"
)

print(
    "Prompt            :",
    repr(
        dispatch[
            "prompt"
        ]
    ),
)

print(
    "CWD               :",
    dispatch[
        "cwd"
    ],
)

print(
    "Repo              :",
    dispatch[
        "repo_root"
    ],
)

print(
    "App Server        : NOT STARTED"
)

print(
    "Model inference   : ZERO"
)


# memory_db may already be closed by cx2_cli.main() finally.
try:
    memory_db.close()
except Exception:
    pass


artifact = {
    "status":
        "ok",

    "failed_rollout": {
        "tool_calls":
            tool_calls,

        "tool_outputs":
            tool_outputs,

        "turn_aborts":
            aborts,

        "token_records":
            len(
                tokens
            ),
    },

    "subprocess": {
        "version":
            version_probe,

        "route":
            route_probe,

        "quota":
            quota_probe,
    },

    "synthetic_dispatch":
        dispatch,

    "crash_log_after_safe_probes":
        crash_text,

    "model_turns":
        0,

    "production_modified":
        False,
}


RESULT.write_text(
    json.dumps(
        artifact,
        ensure_ascii=False,
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)


print()
print(
    "=== BOUNDARY RESULT ==="
)

print(
    "Failed turn tool call    :",
    bool(
        tool_calls
    ),
)

print(
    "Failed turn tool output  :",
    bool(
        tool_outputs
    ),
)

print(
    "--version subprocess     : PASS"
)

print(
    "--route subprocess       : PASS"
)

print(
    "--quota subprocess       : PASS"
)

print(
    "Synthetic one-shot path  : PASS"
)

print(
    "Crash logger             : INSTALLED"
)

print(
    "Model inference          : ZERO"
)
