from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import sys
from typing import Any, Protocol


CX_HOME = Path.home() / ".cx"
PRODUCTION_SRC = CX_HOME / "src"

if str(PRODUCTION_SRC) not in sys.path:
    sys.path.insert(
        0,
        str(PRODUCTION_SRC),
    )


import cx as production_cx


EXPECTED_ROUTER_VERSION = "1.2.1"


def canonical_cwd_key(
    path: str | Path | None,
) -> str:
    return production_cx.canonical_repo_path(path)



class SessionAdapterError(
    RuntimeError
):
    pass


class SessionClient(Protocol):

    def request(
        self,
        method: str,
        params: Any = ...,
        timeout: float = ...,
    ) -> Any:
        ...


def _policy() -> dict[str, Any]:

    version = getattr(
        production_cx,
        "ROUTER_VERSION",
        None,
    )

    if version != EXPECTED_ROUTER_VERSION:
        raise SessionAdapterError(
            "Production router version mismatch: "
            f"{version!r}"
        )

    return production_cx.load_policy()


def detect_repo(
    cwd: Path,
) -> dict[str, Any]:

    return production_cx.detect_repo(
        cwd.resolve()
    )


# =============================================================
# Session state
#
# Production remains the source of truth for:
# - repo key
# - TTL
# - branch checks
# - SQLite schema
# =============================================================

def evaluate_session(
    db: Any,
    repo: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:

    policy = _policy()

    session = (
        production_cx.load_repo_session(
            db,
            repo,
        )
    )

    reusable, reason = (
        production_cx.session_reusable(
            session,
            repo,
            policy,
            now=now,
        )
    )

    age = (
        production_cx.session_age_minutes(
            session,
            now=now,
        )
        if session
        else None
    )

    return {
        "session":
            session,

        "reusable":
            reusable,

        "reason":
            reason,

        "age_minutes":
            age,
    }


# CX2_THREAD_COUNTER_RESET_V1
def save_session(
    db: Any,
    repo: dict[str, Any],
    thread_id: str,
    *,
    context: dict[str, Any] | None = None,
    turns_delta: int = 1,
) -> dict[str, Any] | None:
    """
    Persist CX2 session state with per-thread actual model turn semantics.

    production_cx.save_repo_session() maintains a repo-level row and
    increments an existing user_turns counter unconditionally by 1.
    When multiple actual model turns occur on the same thread (e.g. Turn 1 fix
    + Turn 2 verification continuation), the persisted user_turns counter
    must accurately reflect the total actual model turns executed.
    """
    if not repo.get("git"):
        return None

    existing = production_cx.load_repo_session(
        db,
        repo,
    )

    base_turns = 0
    if isinstance(
        existing,
        dict,
    ):
        existing_thread_id = existing.get(
            "thread_id"
        )

        if (
            str(existing_thread_id or "")
            == str(thread_id)
        ):
            base_turns = int(
                existing.get(
                    "user_turns",
                    0,
                )
            )
        else:
            production_cx.clear_repo_session(
                db,
                repo,
            )

    new_turns = base_turns + max(1, turns_delta)

    production_cx.save_repo_session(
        db,
        repo,
        thread_id,
        context=context,
    )

    key = production_cx.repo_session_key(
        repo
    )
    db.execute(
        "UPDATE sessions SET user_turns = ? WHERE repo_key = ?",
        (
            new_turns,
            key,
        ),
    )
    db.commit()

    return production_cx.load_repo_session(
        db,
        repo,
    )


def clear_session(
    db: Any,
    repo: dict[str, Any],
) -> None:

    production_cx.clear_repo_session(
        db,
        repo,
    )


# =============================================================
# App Server params
# =============================================================

def lean_config() -> dict[str, Any]:

    return {
        "features": {
            "plugins":
                False,

            "apps":
                False,

            "multi_agent":
                False,
        },

        "mcp_servers": {
            "context7": {
                "enabled":
                    False,
            },

            "filesystem": {
                "enabled":
                    False,
            },

            "node_repl": {
                "enabled":
                    False,
            },
        },
    }


# CX2_NATIVE_WEB_CONFIG_V1

CX2_WEB_SEARCH_MODES = {
    "disabled",
    "cached",
    "indexed",
    "live",
}


def lean_config_with_web(
    web_search_mode: str,
) -> dict[str, Any]:

    if web_search_mode not in CX2_WEB_SEARCH_MODES:
        raise ValueError(
            "Unsupported CX2 web search mode: "
            + repr(
                web_search_mode
            )
        )

    config = dict(
        lean_config()
    )

    config[
        "web_search"
    ] = web_search_mode

    return config


def thread_start_params(
    *,
    root: Path,
    model: str,
    permissions: str,
    web_search_mode: str = "disabled",
) -> dict[str, Any]:

    root = root.resolve()

    return {
        "model":
            model,

        "cwd":
            str(root),

        "runtimeWorkspaceRoots": [
            str(root),
        ],

        "approvalPolicy":
            "never",

        "permissions":
            permissions,

        "ephemeral":
            False,

        "config":
            lean_config_with_web(
                web_search_mode
            ),
    }


def thread_resume_params(
    *,
    thread_id: str,
    root: Path,
    model: str,
    permissions: str,
    web_search_mode: str = "disabled",
) -> dict[str, Any]:

    root = root.resolve()

    return {
        "threadId":
            thread_id,

        "model":
            model,

        "cwd":
            str(root),

        "runtimeWorkspaceRoots": [
            str(root),
        ],

        "approvalPolicy":
            "never",

        "permissions":
            permissions,

        "config":
            lean_config_with_web(
                web_search_mode
            ),

        # We do not need full historical turn payload here.
        "excludeTurns":
            True,
    }


# =============================================================
# Response validation
# =============================================================

def validate_thread_response(
    response: Any,
    *,
    expected_permissions: str,
    expected_thread_id: str | None = None,
) -> dict[str, Any]:

    if not isinstance(
        response,
        dict,
    ):
        raise SessionAdapterError(
            "Thread response object değil."
        )

    thread = response.get(
        "thread"
    )

    if not isinstance(
        thread,
        dict,
    ):
        raise SessionAdapterError(
            "Thread response.thread yok."
        )

    thread_id = thread.get(
        "id"
    )

    if (
        not isinstance(
            thread_id,
            str,
        )
        or not thread_id
    ):
        raise SessionAdapterError(
            "Thread id yok."
        )

    if (
        expected_thread_id is not None
        and thread_id
        != expected_thread_id
    ):
        raise SessionAdapterError(
            "Resumed thread id mismatch."
        )

    if (
        thread.get(
            "ephemeral"
        )
        is True
    ):
        raise SessionAdapterError(
            "Persistent session thread ephemeral olamaz."
        )

    profile = response.get(
        "activePermissionProfile"
    )

    if (
        not isinstance(
            profile,
            dict,
        )
        or profile.get(
            "id"
        )
        != expected_permissions
    ):
        raise SessionAdapterError(
            "Permission profile mismatch."
        )

    return {
        "thread":
            thread,

        "thread_id":
            thread_id,

        "active_permission_profile":
            profile,

        "sandbox":
            response.get(
                "sandbox"
            ),

        "runtime_workspace_roots":
            response.get(
                "runtimeWorkspaceRoots"
            ),
    }


# =============================================================
# Start / resume
#
# Mirrors production lifecycle:
#
# reusable session
#   -> thread/resume
#   -> on resume failure clear stored session
#   -> thread/start
#
# non-reusable
#   -> thread/start
#
# IMPORTANT:
# A new thread is NOT saved here.
# The execution layer must call save_session() only after the
# model turn lifecycle reaches the point where production would
# persist the thread.
# =============================================================

# CX2_REUSABLE_WEB_CAPABILITY_V1
def acquire_thread(
    client: SessionClient,
    db: Any,
    repo: dict[str, Any],
    *,
    root: Path,
    model: str,
    permissions: str,
    web_search_mode: str = "disabled",
    active_memory_thread_id: str | None = None,
    reusable: bool = True,
) -> dict[str, Any]:

    effective_start_web_mode = (
        "live"
        if reusable
        else web_search_mode
    )

    effective_resume_web_mode = (
        web_search_mode
    )

    if repo.get("git"):
        decision = evaluate_session(
            db,
            repo,
        )

        session = decision[
            "session"
        ]

        if (
            decision[
                "reusable"
            ]
            and isinstance(
                session,
                dict,
            )
        ):

            stored_thread_id = str(
                session[
                    "thread_id"
                ]
            )

            try:

                response = client.request(
                    "thread/resume",
                    thread_resume_params(
                        thread_id=
                            stored_thread_id,

                        root=
                            root,

                        model=
                            model,

                        permissions=
                            permissions,
                        web_search_mode=
                            effective_resume_web_mode,
                    ),
                    timeout=30.0,
                )

                validated = (
                    validate_thread_response(
                        response,
                        expected_permissions=
                            permissions,

                        expected_thread_id=
                            stored_thread_id,
                    )
                )

                return {
                    "mode":
                        "resume",

                    "reason":
                        decision[
                            "reason"
                        ],

                    "age_minutes":
                        decision[
                            "age_minutes"
                        ],

                    **validated,
                }

            except Exception as exc:

                # Exact production behavior:
                # a stored but unavailable thread is discarded.
                clear_session(
                    db,
                    repo,
                )

                resume_error = repr(
                    exc
                )

            else:
                resume_error = None

        else:
            resume_error = None

        response = client.request(
            "thread/start",
            thread_start_params(
                root=
                    root,

                model=
                    model,

                permissions=
                    permissions,
                web_search_mode=
                    effective_start_web_mode,
            ),
            timeout=30.0,
        )

        validated = (
            validate_thread_response(
                response,
                expected_permissions=
                    permissions,
            )
        )

        return {
            "mode":
                "new",

            "reason":
                decision[
                    "reason"
                ],

            "age_minutes":
                decision[
                    "age_minutes"
                ],

            "resume_error":
                resume_error,

            **validated,
        }

    # -------------------------------------------------------------
    # NON-GIT INTERACTIVE MEMORY CONTINUATION
    # -------------------------------------------------------------
    resume_error = None
    if active_memory_thread_id:
        try:
            response = client.request(
                "thread/resume",
                thread_resume_params(
                    thread_id=
                        active_memory_thread_id,

                    root=
                        root,

                    model=
                        model,

                    permissions=
                        permissions,
                    web_search_mode=
                        effective_resume_web_mode,
                ),
                timeout=30.0,
            )

            validated = (
                validate_thread_response(
                    response,
                    expected_permissions=
                        permissions,

                    expected_thread_id=
                        active_memory_thread_id,
                )
            )

            return {
                "mode":
                    "resume",

                "reason":
                    "memory",

                "age_minutes":
                    None,

                **validated,
            }

        except Exception as exc:
            resume_error = repr(
                exc
            )

    response = client.request(
        "thread/start",
        thread_start_params(
            root=
                root,

            model=
                model,

            permissions=
                permissions,
            web_search_mode=
                effective_start_web_mode,
        ),
        timeout=30.0,
    )

    validated = (
        validate_thread_response(
            response,
            expected_permissions=
                permissions,
        )
    )

    return {
        "mode":
            "new",

        "reason":
            "non_git",

        "age_minutes":
            None,

        "resume_error":
            resume_error,

        **validated,
    }


__all__ = [
    "EXPECTED_ROUTER_VERSION",
    "acquire_thread",
    "canonical_cwd_key",
    "clear_session",
    "detect_repo",
    "evaluate_session",
    "lean_config",
    "save_session",
    "thread_resume_params",
    "thread_start_params",
]

