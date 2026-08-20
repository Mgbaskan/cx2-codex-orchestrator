from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import re
import sqlite3
import subprocess
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


# CX2_NATIVE_HISTORY_MANAGER_V1
# CX2_REPO_SESSION_BINDING_V1
# CX2_HISTORY_VISIBILITY_ISOLATION_V1

THREAD_VISIBILITY_USER = "user"
THREAD_VISIBILITY_INTERNAL_CANARY = "internal_canary"
THREAD_VISIBILITY_INTERNAL_SYNTHETIC = "internal_synthetic"
THREAD_VISIBILITY_UNKNOWN = "unknown"

CANARY_DIR_RE = re.compile(
    r"[\\/]\.cx[\\/]backups[\\/].*?(?:canary|test|verification|smoke|benchmark|precanary|postcanary|hotfix|turnresult|batch\d)",
    re.IGNORECASE,
)

TEMP_DIR_RE = re.compile(
    r"[\\/](?:temp|tmp)[\\/]tmp[0-9a-z_]+",
    re.IGNORECASE,
)

CANARY_PREVIEW_RE = re.compile(
    r"^(?:\[CX2?.*?CANARY\]|CX2?.*?RELEASE CANARY|CX2?.*?REAL CANARY|\[CX2?.*?TEST\]|\[CX2?.*?BENCHMARK\])",
    re.IGNORECASE,
)


class HistoryManagerError(RuntimeError):
    pass


class HistoryClient(Protocol):

    def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        ...


def _canonical_path(
    path_str: str | Path | None,
) -> str:
    return production_cx.canonical_repo_path(path_str)


def classify_thread(
    thread: dict[str, Any],
) -> str:
    """Classify a native thread into:

    - "user" (default visible)
    - "internal_canary" (hidden by default)
    - "internal_synthetic" (hidden by default)
    - "unknown" (fail-open to visible)
    """

    if not isinstance(
        thread,
        dict,
    ):
        return THREAD_VISIBILITY_UNKNOWN

    cwd = _canonical_path(
        thread.get(
            "cwd"
        )
    )

    preview = str(
        thread.get(
            "preview"
        )
        or ""
    ).strip()

    name = str(
        thread.get(
            "name"
        )
        or ""
    ).strip()

    source = str(
        thread.get(
            "source"
        )
        or ""
    ).strip().lower()

    # 1. CWD under ~/.cx/backups/...canary/test/etc...
    if cwd and CANARY_DIR_RE.search(
        cwd
    ):
        return THREAD_VISIBILITY_INTERNAL_CANARY

    # 2. CWD under automated temp directory
    if cwd and (
        TEMP_DIR_RE.search(
            cwd
        )
        or r"\appdata\local\temp\tmp" in cwd
    ):
        return THREAD_VISIBILITY_INTERNAL_SYNTHETIC

    # 3. Explicit canary markers in preview or name
    if preview and CANARY_PREVIEW_RE.search(
        preview
    ):
        return THREAD_VISIBILITY_INTERNAL_CANARY

    if name and CANARY_PREVIEW_RE.search(
        name
    ):
        return THREAD_VISIBILITY_INTERNAL_CANARY

    # 4. Native source metadata if explicitly marked synthetic/test
    if source in (
        "subagentcompact",
        "subagentreview",
        "synthetic",
    ):
        return THREAD_VISIBILITY_INTERNAL_SYNTHETIC

    # Fail-open: default to user
    return THREAD_VISIBILITY_USER


def is_visible_thread(
    thread: dict[str, Any],
    *,
    include_internal: bool = False,
    internal_only: bool = False,
) -> bool:

    classification = classify_thread(
        thread
    )

    is_internal = classification in (
        THREAD_VISIBILITY_INTERNAL_CANARY,
        THREAD_VISIBILITY_INTERNAL_SYNTHETIC,
    )

    if internal_only:
        return is_internal

    if include_internal:
        return True

    return not is_internal


def _unwrap_thread(
    entry: Any,
) -> tuple[dict[str, Any], str | None]:

    if not isinstance(
        entry,
        dict,
    ):
        raise HistoryManagerError(
            "History entry object değil."
        )

    nested = entry.get(
        "thread"
    )

    if isinstance(
        nested,
        dict,
    ):

        snippet = entry.get(
            "snippet"
        )

        return (
            nested,
            str(
                snippet
            ).strip()
            if snippet
            else None,
        )

    return entry, None


def _thread_id(value: Any) -> str:

    result = str(
        value or ""
    ).strip()

    if not result:
        raise HistoryManagerError(
            "Thread id bos olamaz."
        )

    return result


def _limit(value: Any) -> int | None:

    if value is None:
        return None

    try:
        result = int(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise HistoryManagerError(
            "History limit sayi olmali."
        ) from exc

    if result <= 0:
        raise HistoryManagerError(
            "History limit 0'dan buyuk olmali."
        )

    if result > 200:
        raise HistoryManagerError(
            "History limit en fazla 200 olabilir."
        )

    return result


def _response(
    value: Any,
    method: str,
) -> dict[str, Any]:

    if not isinstance(
        value,
        dict,
    ):
        raise HistoryManagerError(
            f"{method}: response object değil."
        )

    return value


def _page(
    value: Any,
    method: str,
) -> dict[str, Any]:

    result = _response(
        value,
        method,
    )

    if not isinstance(
        result.get(
            "data"
        ),
        list,
    ):
        raise HistoryManagerError(
            f"{method}: data list değil."
        )

    for key in (
        "nextCursor",
        "backwardsCursor",
    ):

        cursor = result.get(
            key
        )

        if (
            cursor is not None
            and not isinstance(
                cursor,
                str,
            )
        ):
            raise HistoryManagerError(
                f"{method}: {key} geçersiz."
            )

    return result


def thread_summary(
    thread: dict[str, Any],
) -> dict[str, Any]:

    if not isinstance(
        thread,
        dict,
    ):
        raise HistoryManagerError(
            "Thread object değil."
        )

    thread_id = _thread_id(
        thread.get(
            "id"
        )
    )

    name = thread.get(
        "name"
    )

    preview = str(
        thread.get(
            "preview"
        )
        or ""
    )

    return {
        "id":
            thread_id,

        "name":
            str(
                name
            )
            if name
            else None,

        "preview":
            preview,

        "cwd":
            str(
                thread.get(
                    "cwd"
                )
                or ""
            ),

        "createdAt":
            thread.get(
                "createdAt"
            ),

        "updatedAt":
            thread.get(
                "updatedAt"
            ),

        "recencyAt":
            thread.get(
                "recencyAt"
            ),

        "status":
            thread.get(
                "status"
            ),

        "source":
            thread.get(
                "source"
            ),

        "modelProvider":
            thread.get(
                "modelProvider"
            ),

        "path":
            thread.get(
                "path"
            ),

        "ephemeral":
            bool(
                thread.get(
                    "ephemeral",
                    False,
                )
            ),

        "nameOrPreview":
            (
                str(
                    name
                ).strip()
                if name
                else preview.strip()
            ),

        "classification":
            classify_thread(
                thread
            ),
    }


class NativeHistoryManager:

    def __init__(
        self,
        client: HistoryClient,
        *,
        timeout: float = 30.0,
    ) -> None:

        self.client = client
        self.timeout = float(
            timeout
        )


    def _request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:

        return _response(
            self.client.request(
                method,
                params,
                timeout=self.timeout,
            ),
            method,
        )


    def list_threads(
        self,
        *,
        cwd: Path | str | None = None,
        limit: int | None = 20,
        cursor: str | None = None,
        archived: bool = False,
        search_term: str | None = None,
        include_internal: bool = False,
        internal_only: bool = False,
    ) -> dict[str, Any]:

        target_limit = _limit(limit) or 20

        # When include_internal is requested and not internal_only, single page request is sufficient
        if include_internal and not internal_only:
            params: dict[str, Any] = {
                "archived": bool(archived),
                "limit": target_limit,
            }
            if cursor:
                params["cursor"] = str(cursor)
            if cwd is not None:
                params["cwd"] = str(
                    Path(cwd).expanduser().resolve()
                )
            if search_term is not None and str(search_term).strip():
                params["searchTerm"] = str(search_term).strip()

            return _page(
                self.client.request(
                    "thread/list",
                    params,
                    timeout=self.timeout,
                ),
                "thread/list",
            )

        # Multi-page pagination loop to collect target_limit visible/matching items
        accumulated: list[dict[str, Any]] = []
        current_cursor = cursor
        hidden_count = 0
        max_iterations = 10
        iterations = 0
        seen_cursors: set[str] = set()

        while len(accumulated) < target_limit and iterations < max_iterations:
            iterations += 1
            params = {
                "archived": bool(archived),
                "limit": max(target_limit, 20),
            }
            if current_cursor:
                if current_cursor in seen_cursors:
                    break
                seen_cursors.add(current_cursor)
                params["cursor"] = str(current_cursor)
            if cwd is not None:
                params["cwd"] = str(
                    Path(cwd).expanduser().resolve()
                )
            if search_term is not None and str(search_term).strip():
                params["searchTerm"] = str(search_term).strip()

            raw_page = _page(
                self.client.request(
                    "thread/list",
                    params,
                    timeout=self.timeout,
                ),
                "thread/list",
            )

            raw_data = raw_page.get("data", [])
            next_cursor = raw_page.get("nextCursor")

            for item in raw_data:
                thread_obj, _ = _unwrap_thread(item)
                if is_visible_thread(
                    thread_obj,
                    include_internal=include_internal,
                    internal_only=internal_only,
                ):
                    accumulated.append(item)
                    if len(accumulated) >= target_limit:
                        break
                else:
                    hidden_count += 1

            current_cursor = next_cursor
            if not current_cursor or not raw_data:
                break

        return {
            "data": accumulated[:target_limit],
            "nextCursor": current_cursor if (len(accumulated) >= target_limit or current_cursor) else None,
            "hidden_internal_count": hidden_count,
        }


    def search_threads(
        self,
        search_term: str,
        *,
        limit: int | None = 20,
        cursor: str | None = None,
        archived: bool = False,
        include_internal: bool = False,
        internal_only: bool = False,
    ) -> dict[str, Any]:

        query = str(
            search_term or ""
        ).strip()

        if not query:
            raise HistoryManagerError(
                "History search sorgusu bos olamaz."
            )

        target_limit = _limit(limit) or 20

        if include_internal and not internal_only:
            params: dict[str, Any] = {
                "searchTerm": query,
                "archived": bool(archived),
                "limit": target_limit,
            }
            if cursor:
                params["cursor"] = str(cursor)

            return _page(
                self.client.request(
                    "thread/search",
                    params,
                    timeout=self.timeout,
                ),
                "thread/search",
            )

        accumulated: list[dict[str, Any]] = []
        current_cursor = cursor
        hidden_count = 0
        max_iterations = 10
        iterations = 0
        seen_cursors: set[str] = set()

        while len(accumulated) < target_limit and iterations < max_iterations:
            iterations += 1
            params = {
                "searchTerm": query,
                "archived": bool(archived),
                "limit": max(target_limit, 20),
            }
            if current_cursor:
                if current_cursor in seen_cursors:
                    break
                seen_cursors.add(current_cursor)
                params["cursor"] = str(current_cursor)

            raw_page = _page(
                self.client.request(
                    "thread/search",
                    params,
                    timeout=self.timeout,
                ),
                "thread/search",
            )

            raw_data = raw_page.get("data", [])
            next_cursor = raw_page.get("nextCursor")

            for item in raw_data:
                thread_obj, _ = _unwrap_thread(item)
                if is_visible_thread(
                    thread_obj,
                    include_internal=include_internal,
                    internal_only=internal_only,
                ):
                    accumulated.append(item)
                    if len(accumulated) >= target_limit:
                        break
                else:
                    hidden_count += 1

            current_cursor = next_cursor
            if not current_cursor or not raw_data:
                break

        return {
            "data": accumulated[:target_limit],
            "nextCursor": current_cursor if (len(accumulated) >= target_limit or current_cursor) else None,
            "hidden_internal_count": hidden_count,
        }


    def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = False,
    ) -> dict[str, Any]:

        result = self._request(
            "thread/read",
            {
                "threadId":
                    _thread_id(
                        thread_id
                    ),

                "includeTurns":
                    bool(
                        include_turns
                    ),
            },
        )

        if not isinstance(
            result.get(
                "thread"
            ),
            dict,
        ):
            raise HistoryManagerError(
                "thread/read: thread object yok."
            )

        return result


    def list_turns(
        self,
        thread_id: str,
        *,
        limit: int | None = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:

        params: dict[str, Any] = {
            "threadId":
                _thread_id(
                    thread_id
                )
        }

        checked = _limit(
            limit
        )

        if checked is not None:
            params[
                "limit"
            ] = checked

        if cursor:
            params[
                "cursor"
            ] = str(
                cursor
            )

        return _page(
            self.client.request(
                "thread/turns/list",
                params,
                timeout=self.timeout,
            ),
            "thread/turns/list",
        )


    def loaded_threads(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:

        params: dict[str, Any] = {}

        checked = _limit(
            limit
        )

        if checked is not None:
            params[
                "limit"
            ] = checked

        if cursor:
            params[
                "cursor"
            ] = str(
                cursor
            )

        return _page(
            self.client.request(
                "thread/loaded/list",
                params,
                timeout=self.timeout,
            ),
            "thread/loaded/list",
        )


    def rename_thread(
        self,
        thread_id: str,
        name: str,
    ) -> dict[str, Any]:

        checked_name = str(
            name or ""
        ).strip()

        if not checked_name:
            raise HistoryManagerError(
                "Thread adi bos olamaz."
            )

        return self._request(
            "thread/name/set",
            {
                "threadId":
                    _thread_id(
                        thread_id
                    ),

                "name":
                    checked_name,
            },
        )


    def archive_thread(
        self,
        thread_id: str,
    ) -> dict[str, Any]:

        return self._request(
            "thread/archive",
            {
                "threadId":
                    _thread_id(
                        thread_id
                    )
            },
        )


    def unarchive_thread(
        self,
        thread_id: str,
    ) -> dict[str, Any]:

        result = self._request(
            "thread/unarchive",
            {
                "threadId":
                    _thread_id(
                        thread_id
                    )
            },
        )

        if not isinstance(
            result.get(
                "thread"
            ),
            dict,
        ):
            raise HistoryManagerError(
                "thread/unarchive: thread object yok."
            )

        return result


    def delete_thread(
        self,
        thread_id: str,
    ) -> dict[str, Any]:

        return self._request(
            "thread/delete",
            {
                "threadId":
                    _thread_id(
                        thread_id
                    )
            },
        )


def bind_repo_session(
    db: sqlite3.Connection,
    repo: dict[str, Any],
    thread_id: str,
) -> dict[str, Any]:

    if not repo.get(
        "git"
    ):
        raise HistoryManagerError(
            "History thread secimi icin Git repo gerekli."
        )

    checked_id = _thread_id(
        thread_id
    )

    production_cx.init_session_table(
        db
    )

    repo_key = (
        production_cx.repo_session_key(
            repo
        )
    )

    branch = (
        production_cx.current_repo_branch(
            repo
        )
    )

    now = datetime.now(
        timezone.utc
    ).isoformat()

    db.execute(
        """
        INSERT INTO sessions (
            repo_key,
            repo_root,
            thread_id,
            branch,
            last_used_at,
            user_turns,
            context_tokens,
            context_window,
            context_percent
        )
        VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, NULL)

        ON CONFLICT(repo_key)
        DO UPDATE SET
            repo_root = excluded.repo_root,
            thread_id = excluded.thread_id,
            branch = excluded.branch,
            last_used_at = excluded.last_used_at,
            user_turns = 0,
            context_tokens = NULL,
            context_window = NULL,
            context_percent = NULL
        """,
        (
            repo_key,
            str(
                repo[
                    "root"
                ]
            ),
            checked_id,
            branch,
            now,
        ),
    )

    db.commit()

    result = (
        production_cx.load_repo_session(
            db,
            repo,
        )
    )

    if not isinstance(
        result,
        dict,
    ):
        raise HistoryManagerError(
            "Selected repo session okunamadi."
        )

    if (
        str(
            result.get(
                "thread_id"
            )
            or ""
        )
        != checked_id
    ):
        raise HistoryManagerError(
            "Selected thread persist edilmedi."
        )

    return result


def unbind_repo_session(
    db: sqlite3.Connection,
    repo: dict[str, Any],
) -> None:

    production_cx.clear_repo_session(
        db,
        repo,
    )

# =============================================================
# Native delete compatibility
# =============================================================

# CX2_NATIVE_DELETE_COMPATIBILITY_V1
#
# CX production intentionally shares ~/.codex with Codex Desktop
# for authentication and native thread history.
#
# codex-cli 0.144.4 creates state schema through migration 40 and
# expects agent_jobs during thread/delete.
#
# Newer Codex Desktop state schema migration 42 intentionally drops
# agent_jobs. In that mixed-version state, 0.144.4 thread/delete is
# unsafe/incompatible and must fail closed rather than mutating the
# shared Codex state store.


def native_delete_compatibility(
    *,
    codex_home: Path | str | None = None,
    codex_exe: Path | str | None = None,
) -> dict[str, Any]:
    from codex_compat import evaluate_native_delete_safety

    return evaluate_native_delete_safety(
        codex_home=codex_home,
        codex_exe=codex_exe,
    )
