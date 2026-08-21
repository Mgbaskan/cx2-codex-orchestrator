from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys


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

from cx2_runtime import (
    CX2Runtime,
    EXPECTED_ROUTER_VERSION,
    RUNTIME_VERSION,
)

from session_adapter import (
    detect_repo,
)


REPO_ROOT = Path(
    r"C:\Projects\docker_projects\hibrit_app"
).resolve()

PRODUCTION_DB = (
    CX_HOME
    / "data"
    / "usage.sqlite3"
)

TEMP_DB = Path(
    sys.argv[1]
).resolve()

RESULT = (
    STAGE
    / "cx2-runtime-canary-last.json"
)


PROMPT = (
    "Bu repoda package.json dosyasi var mi? "
    "Sadece EVET veya HAYIR yaz."
)


def create_isolated_db() -> None:
    """
    Create a transactionally consistent SQLite copy.

    Source production DB is only read.
    All canary telemetry/session writes go to TEMP_DB.
    """

    if TEMP_DB.exists():
        TEMP_DB.unlink()

    source = sqlite3.connect(
        str(PRODUCTION_DB)
    )

    target = sqlite3.connect(
        str(TEMP_DB)
    )

    try:
        source.backup(
            target
        )
    finally:
        target.close()
        source.close()


def main() -> int:

    print(
        "=== CX2 REAL RUNTIME CANARY ==="
    )

    if (
        production_cx.ROUTER_VERSION
        != "1.2.1"
    ):
        raise RuntimeError(
            "Production router 1.2.1 degil."
        )

    if (
        EXPECTED_ROUTER_VERSION
        != "1.2.1"
    ):
        raise RuntimeError(
            "Runtime guard 1.2.1 degil."
        )

    print(
        "Router version    :",
        production_cx.ROUTER_VERSION,
    )

    print(
        "Runtime version   :",
        RUNTIME_VERSION,
    )

    # =========================================================
    # Isolated DB
    # =========================================================

    create_isolated_db()

    if not TEMP_DB.exists():
        raise RuntimeError(
            "Isolated SQLite DB olusmadi."
        )

    print(
        "Production DB     : READ-ONLY SOURCE"
    )

    print(
        "Canary DB         :",
        TEMP_DB,
    )

    repo = detect_repo(
        REPO_ROOT
    )

    if not repo.get(
        "git"
    ):
        raise RuntimeError(
            "HIBRIT git repo olarak algilanmadi."
        )

    db = sqlite3.connect(
        str(TEMP_DB)
    )

    runtime = None

    try:
        # -----------------------------------------------------
        # Force NEW thread in the isolated DB.
        #
        # This prevents the canary from touching/resuming the
        # user's currently stored production HIBRIT session.
        # -----------------------------------------------------

        production_cx.clear_repo_session(
            db,
            repo,
        )

        assert (
            production_cx.load_repo_session(
                db,
                repo,
            )
            is None
        )

        turns_before = db.execute(
            "SELECT COUNT(*) FROM turns"
        ).fetchone()[0]

        print()
        print(
            "Isolated session : CLEARED"
        )

        print(
            "Turns before     :",
            turns_before,
        )

        # =====================================================
        # REAL CX2 RUNTIME
        # =====================================================

        runtime = CX2Runtime(
            live=True
        )

        result = runtime.execute_prompt(
            prompt=PROMPT,
            cwd=REPO_ROOT,
            repo=repo,
            db=db,
        )

        # =====================================================
        # Validation
        # =====================================================

        if result.blocked:

            print()
            print(
                "CANARY BLOCKED BY LIVE BUDGET GUARD"
            )

            print(
                "No model validation performed."
            )

            artifact = {
                "status":
                    "blocked",

                "runtime_version":
                    RUNTIME_VERSION,

                "router_version":
                    EXPECTED_ROUTER_VERSION,

                "quota":
                    result.quota,

                "model_turns":
                    0,

                "production_db_modified":
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

            return 2

        if result.final_result is None:
            raise RuntimeError(
                "Canary final_result yok."
            )

        if result.raw_turn_result is None:
            raise RuntimeError(
                "Canary raw_turn_result yok."
            )

        answer = (
            result.final_result
            .final_response
            .strip()
        )

        if answer.casefold() != "evet":
            raise RuntimeError(
                "Beklenen EVET, gelen: "
                + repr(
                    answer
                )
            )

        if (
            result.final_result
            .status
            .value
            != "completed"
        ):
            raise RuntimeError(
                "Turn completed degil: "
                + repr(
                    result.final_result
                    .status
                    .value
                )
            )

        if result.attempts_used != 1:
            raise RuntimeError(
                "Canary 1 attempt disinda calisti."
            )

        if result.escalations != 0:
            raise RuntimeError(
                "Canary escalation kullandi."
            )

        route = result.plan[
            "route"
        ]

        attempts = result.plan[
            "attempts"
        ]

        first_attempt = attempts[0]

        if (
            first_attempt[
                "tier"
            ]
            != "routine"
        ):
            raise RuntimeError(
                "Canary routine degil."
            )

        if (
            first_attempt[
                "model"
            ]
            != "gpt-5.6-luna"
        ):
            raise RuntimeError(
                "Canary Luna degil."
            )

        if (
            first_attempt[
                "reasoning"
            ]
            != "low"
        ):
            raise RuntimeError(
                "Canary reasoning low degil."
            )

        if (
            first_attempt[
                "permissions"
            ]
            != ":read-only"
        ):
            raise RuntimeError(
                "Canary read-only degil."
            )

        raw = result.raw_turn_result

        if getattr(
            raw,
            "latest_diff",
            "",
        ):
            raise RuntimeError(
                "Read-only canary diff uretti."
            )

        # -----------------------------------------------------
        # Verify telemetry was persisted to isolated DB.
        # -----------------------------------------------------

        turns_after = db.execute(
            "SELECT COUNT(*) FROM turns"
        ).fetchone()[0]

        if (
            turns_after
            != turns_before + 1
        ):
            raise RuntimeError(
                "Isolated turns table beklenen +1 olmadi."
            )

        saved_session = (
            production_cx.load_repo_session(
                db,
                repo,
            )
        )

        if not isinstance(
            saved_session,
            dict,
        ):
            raise RuntimeError(
                "Canary session persist edilmedi."
            )

        if (
            saved_session[
                "thread_id"
            ]
            != result.thread_id
        ):
            raise RuntimeError(
                "Saved thread mismatch."
            )

        if (
            saved_session[
                "user_turns"
            ]
            != 1
        ):
            raise RuntimeError(
                "Expected isolated user_turns=1, got "
                + repr(
                    saved_session[
                        "user_turns"
                    ]
                )
            )

        context_tokens = (
            saved_session.get(
                "context_tokens"
            )
        )

        context_window = (
            saved_session.get(
                "context_window"
            )
        )

        context_percent = (
            saved_session.get(
                "context_percent"
            )
        )

        if not isinstance(
            context_tokens,
            int,
        ) or context_tokens <= 0:

            raise RuntimeError(
                "Session context_tokens invalid."
            )

        if not isinstance(
            context_window,
            int,
        ) or context_window <= 0:

            raise RuntimeError(
                "Session context_window invalid."
            )

        token_usage = getattr(
            raw,
            "token_usage",
            {},
        )

        last_usage = (
            token_usage.get(
                "last",
                {},
            )
            if isinstance(
                token_usage,
                dict,
            )
            else {}
        )

        cached_input = (
            last_usage.get(
                "cachedInputTokens"
            )
        )

        print()
        print(
            "=== CANARY VALIDATION ==="
        )

        print(
            "Thread id        :",
            result.thread_id,
        )

        print(
            "Session mode     :",
            result.session_mode,
        )

        print(
            "Answer           :",
            repr(
                answer
            ),
        )

        print(
            "Status           :",
            result.final_result
            .status
            .value,
        )

        print(
            "Attempts used    :",
            result.attempts_used,
        )

        print(
            "Escalations      :",
            result.escalations,
        )

        print(
            "Tier             :",
            first_attempt[
                "tier"
            ],
        )

        print(
            "Model            :",
            first_attempt[
                "model"
            ],
        )

        print(
            "Reasoning        :",
            first_attempt[
                "reasoning"
            ],
        )

        print(
            "Permissions      :",
            first_attempt[
                "permissions"
            ],
        )

        print(
            "Diff chars       :",
            len(
                getattr(
                    raw,
                    "latest_diff",
                    "",
                )
            ),
        )

        print(
            "Turns before     :",
            turns_before,
        )

        print(
            "Turns after      :",
            turns_after,
        )

        print(
            "Saved user turns :",
            saved_session[
                "user_turns"
            ],
        )

        print(
            "Context tokens   :",
            context_tokens,
        )

        print(
            "Context window   :",
            context_window,
        )

        print(
            "Context percent  :",
            (
                f"{context_percent:.4f}%"
                if isinstance(
                    context_percent,
                    (int, float),
                )
                else "?"
            ),
        )

        print(
            "Cached input     :",
            cached_input,
        )

        print(
            "Runtime canary   : PASS"
        )

        artifact = {
            "status":
                "ok",

            "runtime_version":
                RUNTIME_VERSION,

            "router_version":
                EXPECTED_ROUTER_VERSION,

            "thread_id":
                result.thread_id,

            "session_mode":
                result.session_mode,

            "answer":
                answer,

            "turn_status":
                result.final_result
                .status
                .value,

            "attempts_used":
                result.attempts_used,

            "escalations":
                result.escalations,

            "quota":
                result.quota,

            "route":
                route,

            "selected_attempt":
                first_attempt,

            "diff_chars":
                len(
                    getattr(
                        raw,
                        "latest_diff",
                        "",
                    )
                ),

            "token_usage":
                token_usage,

            "turns_before":
                turns_before,

            "turns_after":
                turns_after,

            "saved_session":
                saved_session,

            "production_db_used_for_writes":
                False,

            "canary_db":
                str(
                    TEMP_DB
                ),

            "thread_start_called":
                result.session_mode
                == "new",

            "turn_start_called":
                True,

            "model_turns":
                1,

            "auto_escalation_used":
                result.escalations > 0,
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

    finally:

        if runtime is not None:
            runtime.close()

        db.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
