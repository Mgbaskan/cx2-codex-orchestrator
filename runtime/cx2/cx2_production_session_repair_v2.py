from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys


DB_PATH = Path(sys.argv[1]).resolve()
BACKUP_PATH = Path(sys.argv[2]).resolve()
REPO_ROOT = Path(sys.argv[3]).resolve()
EXPECTED_THREAD = sys.argv[4]
RESULT_PATH = Path(sys.argv[5]).resolve()

EXPECTED_TURNS = 12
EXPECTED_TURN_ID = 15
EXPECTED_CONTEXT_TOKENS = 10887
EXPECTED_CONTEXT_WINDOW = 258400


def digest_rows(rows) -> str:
    return hashlib.sha256(
        repr(rows).encode(
            "utf-8",
            errors="replace",
        )
    ).hexdigest()


def session_columns(db):
    return [
        row[1]
        for row in db.execute(
            "PRAGMA table_info(sessions)"
        ).fetchall()
    ]


def read_session(db):
    columns = session_columns(db)

    rows = db.execute(
        """
        SELECT *
        FROM sessions
        WHERE lower(repo_root) = lower(?)
        """,
        (str(REPO_ROOT),),
    ).fetchall()

    if len(rows) != 1:
        raise RuntimeError(
            f"Exactly one repo session expected, got {len(rows)}"
        )

    return dict(
        zip(
            columns,
            rows[0],
        )
    )


def latest_turn(db):
    return db.execute(
        """
        SELECT
            id,
            thread_id,
            route,
            score,
            model,
            effort,
            sandbox,
            status,
            input_tokens,
            cached_input_tokens,
            output_tokens,
            reasoning_output_tokens
        FROM turns
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


def validate_turn(row):
    if row is None:
        raise RuntimeError(
            "Latest telemetry turn missing."
        )

    (
        turn_id,
        thread_id,
        route,
        score,
        model,
        effort,
        sandbox,
        status,
        input_tokens,
        cached_input_tokens,
        output_tokens,
        reasoning_tokens,
    ) = row

    expected = {
        "turn_id": (turn_id, EXPECTED_TURN_ID),
        "thread_id": (thread_id, EXPECTED_THREAD),
        "route": (route, "routine"),
        "score": (score, 0),
        "model": (model, "gpt-5.6-luna"),
        "effort": (effort, "low"),
        "sandbox": (sandbox, "read-only"),
        "status": (str(status), "completed"),
    }

    for name, pair in expected.items():
        actual, wanted = pair

        if actual != wanted:
            raise RuntimeError(
                f"{name}: expected {wanted!r}, got {actual!r}"
            )

    if not isinstance(input_tokens, int) or input_tokens <= 0:
        raise RuntimeError(
            "Invalid input token count."
        )

    if not isinstance(output_tokens, int) or output_tokens <= 0:
        raise RuntimeError(
            "Invalid output token count."
        )


def backup_database():
    if BACKUP_PATH.exists():
        BACKUP_PATH.unlink()

    source = sqlite3.connect(
        str(DB_PATH)
    )

    target = sqlite3.connect(
        str(BACKUP_PATH)
    )

    try:
        source.execute(
            "PRAGMA query_only = ON"
        )

        source.backup(
            target
        )

    finally:
        target.close()
        source.close()

    if not BACKUP_PATH.exists():
        raise RuntimeError(
            "SQLite backup creation failed."
        )


def main():
    print(
        "=== GUARDED DB STATE CHECK ==="
    )

    backup_database()

    print(
        "DB backup        :",
        BACKUP_PATH,
    )

    db = sqlite3.connect(
        str(DB_PATH)
    )

    db.execute(
        "PRAGMA busy_timeout = 5000"
    )

    action = None
    before_counter = None
    after_counter = None

    try:
        db.execute(
            "BEGIN IMMEDIATE"
        )

        triggers = db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE
                type = 'trigger'
                AND lower(tbl_name) = 'sessions'
            """
        ).fetchall()

        if triggers:
            raise RuntimeError(
                "sessions table has trigger(s): "
                + repr(triggers)
            )

        turn_count = int(
            db.execute(
                "SELECT COUNT(*) FROM turns"
            ).fetchone()[0]
        )

        if turn_count != EXPECTED_TURNS:
            raise RuntimeError(
                f"Expected {EXPECTED_TURNS} turns, got {turn_count}. "
                "State changed; repair aborted."
            )

        turn_before = latest_turn(
            db
        )

        validate_turn(
            turn_before
        )

        session_before = read_session(
            db
        )

        if session_before.get("thread_id") != EXPECTED_THREAD:
            raise RuntimeError(
                "Current session thread is not the successful smoke thread."
            )

        if int(session_before.get("context_tokens", -1)) != EXPECTED_CONTEXT_TOKENS:
            raise RuntimeError(
                "context_tokens changed; repair aborted."
            )

        if int(session_before.get("context_window", -1)) != EXPECTED_CONTEXT_WINDOW:
            raise RuntimeError(
                "context_window changed; repair aborted."
            )

        before_counter = int(
            session_before.get(
                "user_turns",
                -1,
            )
        )

        if before_counter not in (1, 8):
            raise RuntimeError(
                f"Unexpected user_turns={before_counter}; expected 8 or 1."
            )

        all_turns_before = db.execute(
            """
            SELECT *
            FROM turns
            ORDER BY id
            """
        ).fetchall()

        turn_digest_before = digest_rows(
            all_turns_before
        )

        session_without_counter_before = {
            key: value
            for key, value in session_before.items()
            if key != "user_turns"
        }

        if before_counter == 8:
            cursor = db.execute(
                """
                UPDATE sessions
                SET user_turns = 1
                WHERE
                    repo_key = ?
                    AND thread_id = ?
                    AND user_turns = 8
                """,
                (
                    session_before["repo_key"],
                    EXPECTED_THREAD,
                ),
            )

            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Expected one repaired row, got {cursor.rowcount}"
                )

            action = "repaired"

        else:
            action = "already-correct"

        session_after = read_session(
            db
        )

        after_counter = int(
            session_after.get(
                "user_turns",
                -1,
            )
        )

        if after_counter != 1:
            raise RuntimeError(
                f"user_turns after repair is {after_counter}, expected 1."
            )

        session_without_counter_after = {
            key: value
            for key, value in session_after.items()
            if key != "user_turns"
        }

        if session_without_counter_before != session_without_counter_after:
            raise RuntimeError(
                "A session field other than user_turns changed."
            )

        all_turns_after = db.execute(
            """
            SELECT *
            FROM turns
            ORDER BY id
            """
        ).fetchall()

        turn_digest_after = digest_rows(
            all_turns_after
        )

        if turn_digest_before != turn_digest_after:
            raise RuntimeError(
                "Telemetry turns changed during repair."
            )

        db.commit()

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()

    # ---------------------------------------------------------
    # Post-commit read-only verification
    # ---------------------------------------------------------

    verify = sqlite3.connect(
        f"file:{DB_PATH}?mode=ro",
        uri=True,
    )

    try:
        committed_turn_count = int(
            verify.execute(
                "SELECT COUNT(*) FROM turns"
            ).fetchone()[0]
        )

        committed_turn = latest_turn(
            verify
        )

        committed_session = read_session(
            verify
        )

        committed_turns = verify.execute(
            """
            SELECT *
            FROM turns
            ORDER BY id
            """
        ).fetchall()

    finally:
        verify.close()

    validate_turn(
        committed_turn
    )

    if committed_turn_count != EXPECTED_TURNS:
        raise RuntimeError(
            "Post-commit turn count changed."
        )

    if committed_session.get("thread_id") != EXPECTED_THREAD:
        raise RuntimeError(
            "Post-commit thread changed."
        )

    if int(committed_session.get("user_turns", -1)) != 1:
        raise RuntimeError(
            "Post-commit user_turns != 1."
        )

    if digest_rows(committed_turns) != turn_digest_before:
        raise RuntimeError(
            "Post-commit telemetry digest changed."
        )

    print()
    print(
        "=== REPAIR VALIDATION ==="
    )

    print(
        "Action            :",
        action,
    )

    print(
        "Thread            :",
        EXPECTED_THREAD,
    )

    print(
        "user_turns before :",
        before_counter,
    )

    print(
        "user_turns after  :",
        committed_session["user_turns"],
    )

    print(
        "Context tokens    :",
        committed_session["context_tokens"],
    )

    print(
        "Context window    :",
        committed_session["context_window"],
    )

    print(
        "Context percent   :",
        committed_session["context_percent"],
    )

    print(
        "Turns count       :",
        committed_turn_count,
    )

    print(
        "Latest turn id    :",
        committed_turn[0],
    )

    print(
        "Turns table       : UNCHANGED"
    )

    print(
        "Other fields      : UNCHANGED"
    )

    print(
        "Repair validation : PASS"
    )

    artifact = {
        "status": "ok",
        "action": action,
        "thread_id": EXPECTED_THREAD,

        "user_turns": {
            "before": before_counter,
            "after": int(
                committed_session["user_turns"]
            ),
        },

        "session": {
            "context_tokens":
                committed_session["context_tokens"],

            "context_window":
                committed_session["context_window"],

            "context_percent":
                committed_session["context_percent"],

            "last_used_at":
                committed_session["last_used_at"],

            "branch":
                committed_session["branch"],
        },

        "telemetry": {
            "turn_count":
                committed_turn_count,

            "latest_turn_id":
                committed_turn[0],

            "latest_thread_id":
                committed_turn[1],

            "turns_digest_unchanged":
                True,
        },

        "backup":
            str(BACKUP_PATH),

        "model_turns":
            0,

        "app_server_started":
            False,
    }

    RESULT_PATH.write_text(
        json.dumps(
            artifact,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
