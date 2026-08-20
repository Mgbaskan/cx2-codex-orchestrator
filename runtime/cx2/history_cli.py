from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from typing import Any


CX_HOME = Path.home() / ".cx"
PRODUCTION_SRC = CX_HOME / "src"

if str(PRODUCTION_SRC) not in sys.path:
    sys.path.insert(
        0,
        str(PRODUCTION_SRC),
    )

import cx as production_cx

from history_manager import (
    HistoryManagerError,
    NativeHistoryManager,
    bind_repo_session,
    native_delete_compatibility,
    thread_summary,
    unbind_repo_session,
)

from selection_context import SelectionContext
import session_adapter


# CX2_HISTORY_CLI_V1
# CX2_HISTORY_DESTRUCTIVE_CONFIRM_V1
# CX2_HISTORY_REPO_SCOPE_V1
# CX2_NUMERIC_SELECTION_V1


ACTIVE_SELECTION_CONTEXT = SelectionContext()


HISTORY_COMMANDS = {
    "/history",
    "/search",
    "/thread",
    "/turns",
    "/resume",
    "/rename",
    "/archive",
    "/unarchive",
    "/delete",
}


def _command(
    text: str,
) -> str:

    parts = text.strip().split(
        maxsplit=1
    )

    if not parts:
        return ""

    return parts[0].casefold()


def is_history_command(
    text: str,
) -> bool:

    return (
        _command(
            text
        )
        in HISTORY_COMMANDS
    )


def _manager(
    runtime: Any,
) -> NativeHistoryManager:

    if runtime is None:
        raise HistoryManagerError(
            "History komutları interaktif runtime gerektiriyor."
        )

    runtime.start()

    client = getattr(
        runtime,
        "client",
        None,
    )

    if client is None:
        raise HistoryManagerError(
            "History App Server client bulunamadı."
        )

    return NativeHistoryManager(
        client
    )


def _normalize_path(
    value: str | Path,
) -> str:
    return production_cx.canonical_repo_path(value)


def _same_path(
    left: str | Path,
    right: str | Path,
) -> bool:
    return (
        _normalize_path(
            left
        )
        == _normalize_path(
            right
        )
    )


def _format_time(
    value: Any,
) -> str:

    if isinstance(
        value,
        (int, float),
    ):

        try:

            dt = datetime.fromtimestamp(
                float(
                    value
                ),
                tz=timezone.utc,
            ).astimezone()

            return dt.strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        except (
            OSError,
            OverflowError,
            ValueError,
        ):
            pass

    return (
        str(
            value
        )
        if value is not None
        else "-"
    )


def _terminal_width() -> int:
    try:
        return max(40, shutil.get_terminal_size((80, 24)).columns)
    except Exception:
        return 80


def _format_relative_time(
    value: Any,
) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
            now = datetime.now(timezone.utc)
            diff = (now - dt).total_seconds()
            if diff < 60:
                return "şimdi"
            if diff < 3600:
                return f"{int(diff // 60)} dk"
            if diff < 86400:
                return f"{int(diff // 3600)} sa"
            if diff < 172800:
                return "dün"
            if diff < 86400 * 30:
                return f"{int(diff // 86400)} gün"
            if diff < 86400 * 365:
                return f"{int(diff // (86400 * 30))} ay"
            return f"{int(diff // (86400 * 365))} yıl"
        except (OSError, OverflowError, ValueError):
            pass
    return _format_time(value)


def _compact_path(
    path_str: str,
    max_len: int = 30,
) -> str:
    if not path_str or path_str == "-":
        return "-"
    norm = path_str.replace("/", "\\") if "\\" in path_str or (len(path_str) > 1 and path_str[1] == ":") else path_str
    if len(norm) <= max_len:
        return norm

    sep = "\\" if "\\" in norm else "/"
    parts = [p for p in norm.split(sep) if p]
    if len(parts) <= 1:
        return norm[:max(3, max_len - 3)] + "..."

    shortened = f"...{sep}{parts[-2]}{sep}{parts[-1]}"
    if len(shortened) <= max_len:
        return shortened

    shortened = f"...{sep}{parts[-1]}"
    if len(shortened) <= max_len:
        return shortened

    return shortened[:max(3, max_len - 3)] + "..."


def _clean_title(
    title: str,
) -> str:
    if not title:
        return "(isimsiz thread)"
    cleaned = re.sub(r"[\r\n\t]+", " ", str(title)).strip()
    return cleaned if cleaned else "(isimsiz thread)"


def _truncate(
    text: str,
    max_len: int,
) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[:max_len - 3] + "..."


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


def _print_threads(
    response: dict[str, Any],
    *,
    title: str,
    show_classification: bool = False,
    source: str = "history",
) -> None:

    data = response.get(
        "data",
        [],
    )

    print(
        title
    )

    print()

    if not data:

        ACTIVE_SELECTION_CONTEXT.clear()

        print(
            "[cx] Thread bulunamadı."
        )

        hidden_count = response.get(
            "hidden_internal_count",
            0,
        )

        if hidden_count > 0:
            print(
                f"[cx] {hidden_count} dahili/canary thread gizlendi."
            )

        return

    displayed_entries: list[dict[str, Any]] = []
    width = _terminal_width()
    num_digits = len(str(len(data)))

    for index, entry in enumerate(
        data,
        start=1,
    ):

        thread, snippet = (
            _unwrap_thread(
                entry
            )
        )

        summary = thread_summary(
            thread
        )
        displayed_entries.append(summary)

        raw_label = (
            summary[
                "nameOrPreview"
            ]
            or "(isimsiz thread)"
        )

        classification = summary.get(
            "classification",
            "user",
        )

        if show_classification or classification != "user":
            raw_label = f"[{classification}] {raw_label}"

        clean_label = _clean_title(raw_label)
        selector = f"[{index:>{num_digits}}]"
        age_str = _format_relative_time(summary.get("updatedAt"))
        status_str = str(summary.get("status") or "-")
        raw_cwd = str(summary.get("cwd") or "-")

        if width >= 120:
            cwd_max = min(40, max(20, width // 4))
            cwd_str = _compact_path(raw_cwd, cwd_max)
            age_part = f"{age_str:>7}"
            status_part = f"{status_str:<10}"
            fixed_len = len(selector) + 1 + len(age_part) + 2 + len(status_part) + 2 + len(cwd_str) + 2
            title_max = max(15, width - fixed_len)
            title_part = _truncate(clean_label, title_max)
            row = f"{selector} {title_part:<{title_max}}  {age_part}  {status_part}  {cwd_str}"
        elif width >= 80:
            cwd_str = _compact_path(raw_cwd, 22)
            age_part = f"{age_str:>6}"
            status_part = f"{status_str:<9}"
            fixed_len = len(selector) + 1 + len(age_part) + 2 + len(status_part) + 2 + len(cwd_str) + 2
            title_max = max(15, width - fixed_len)
            title_part = _truncate(clean_label, title_max)
            row = f"{selector} {title_part:<{title_max}}  {age_part}  {status_part}  {cwd_str}"
        elif width >= 60:
            age_part = f"{age_str:>6}"
            status_part = f"{status_str:<9}"
            fixed_len = len(selector) + 1 + len(age_part) + 2 + len(status_part) + 2
            title_max = max(12, width - fixed_len)
            title_part = _truncate(clean_label, title_max)
            row = f"{selector} {title_part:<{title_max}}  {age_part}  {status_part}"
        else:
            age_part = f"{age_str:>5}"
            fixed_len = len(selector) + 1 + len(age_part) + 1
            title_max = max(8, width - fixed_len)
            title_part = _truncate(clean_label, title_max)
            row = f"{selector} {title_part} {age_part}"

        print(row)

        if snippet:
            match_clean = _clean_title(snippet)
            match_trunc = _truncate(match_clean, max(20, width - 14))
            print(f"    → match: {match_trunc}")

    print()

    ACTIVE_SELECTION_CONTEXT.set_entries(
        source=source,
        title=title,
        entries=displayed_entries,
    )

    cursor = response.get(
        "nextCursor"
    )

    if cursor:

        print(
            "[cx] Daha fazla sonuç var."
        )

    hidden_count = response.get(
        "hidden_internal_count",
        0,
    )

    if hidden_count > 0 and not show_classification:
        print(
            f"[cx] {hidden_count} dahili/canary thread gizlendi."
        )

    if source == "search":
        print(
            "[cx] Kullanım: /resume 1 | /thread 1"
        )
    else:
        print(
            "[cx] Kullanım: /resume 1 | /thread 1 | /archive 1"
        )


def _current_thread_id(
    db: sqlite3.Connection,
    repo: dict[str, Any],
) -> str | None:

    decision = (
        session_adapter.evaluate_session(
            db,
            repo,
        )
    )

    session = decision.get(
        "session"
    )

    if not isinstance(
        session,
        dict,
    ):
        return None

    value = str(
        session.get(
            "thread_id"
        )
        or ""
    ).strip()

    return value or None


def _resolve_read_thread_id(
    argument: str,
    db: sqlite3.Connection,
    repo: dict[str, Any],
) -> str:

    value = argument.strip()

    if value:
        return ACTIVE_SELECTION_CONTEXT.resolve(
            value
        )

    selected = _current_thread_id(
        db,
        repo,
    )

    if not selected:
        raise HistoryManagerError(
            "Aktif repo thread'i yok."
        )

    return selected


def _unbind_if_selected(
    db: sqlite3.Connection,
    repo: dict[str, Any],
    thread_id: str,
) -> bool:

    selected = _current_thread_id(
        db,
        repo,
    )

    if (
        selected is None
        or selected != thread_id
    ):
        return False

    unbind_repo_session(
        db,
        repo,
    )

    return True


def _confirm_delete(
    thread_id: str,
) -> bool:

    if (
        not sys.stdin.isatty()
        or not sys.stdout.isatty()
    ):

        print(
            "[cx] Silme onayı TTY gerektirir; SİLME REDDEDİLDİ."
        )

        return False

    expected = (
        "DELETE "
        + thread_id
    )

    print(
        "[cx] Bu işlem Codex thread'ini kalıcı olarak siler."
    )

    try:

        answer = input(
            "[cx] Onay için tam olarak "
            + repr(
                expected
            )
            + " yaz: "
        )

    except (
        EOFError,
        KeyboardInterrupt,
    ):

        print()

        print(
            "[cx] Silme iptal edildi."
        )

        return False

    if answer.strip() != expected:

        print(
            "[cx] Onay eşleşmedi; silme iptal edildi."
        )

        return False

    return True


def _print_thread(
    thread: dict[str, Any],
) -> None:

    summary = thread_summary(
        thread
    )

    print(
        "=== CX THREAD ==="
    )

    print(
        "ID        :",
        summary[
            "id"
        ],
    )

    print(
        "Ad        :",
        summary[
            "name"
        ]
        or "-",
    )

    print(
        "Önizleme  :",
        summary[
            "preview"
        ]
        or "-",
    )

    print(
        "Konum     :",
        summary[
            "cwd"
        ]
        or "-",
    )

    print(
        "Durum     :",
        summary[
            "status"
        ]
        or "-",
    )

    print(
        "Oluşturma :",
        _format_time(
            summary[
                "createdAt"
            ]
        ),
    )

    print(
        "Güncelleme:",
        _format_time(
            summary[
                "updatedAt"
            ]
        ),
    )

    print(
        "Kaynak    :",
        summary[
            "source"
        ]
        or "-",
    )

    print(
        "Model     :",
        summary[
            "modelProvider"
        ]
        or "-",
    )


def _print_turns(
    response: dict[str, Any],
    *,
    thread_id: str,
) -> None:

    data = response.get(
        "data",
        [],
    )

    print(
        "=== CX THREAD TURNS ==="
    )

    print(
        "Thread :",
        thread_id,
    )

    print()

    if not data:

        print(
            "[cx] Turn bulunamadı."
        )

        return

    for index, turn in enumerate(
        data,
        start=1,
    ):

        if not isinstance(
            turn,
            dict,
        ):
            continue

        items = turn.get(
            "items"
        )

        item_count = (
            len(
                items
            )
            if isinstance(
                items,
                list,
            )
            else 0
        )

        status = turn.get("status", "-")
        duration = turn.get("durationMs")
        dur_str = f"{duration}ms" if duration is not None else "-"
        if isinstance(duration, (int, float)) and duration >= 1000:
            dur_str = f"{duration / 1000:.1f}s"

        print(
            f"[{index}] {status} · {dur_str} · {item_count} öğe"
        )


def _usage(
    text: str,
) -> None:

    print(
        text
    )


def handle_history_command(
    value: str,
    *,
    runtime: Any,
    db: sqlite3.Connection,
    cwd: Path,
    repo: dict[str, Any],
) -> bool:

    text = value.strip()
    command = _command(
        text
    )

    if command not in HISTORY_COMMANDS:
        return False

    try:

        # =====================================================
        # /history
        # =====================================================

        if command == "/history":

            folded = text.casefold()

            if folded == "/history":

                manager = _manager(
                    runtime
                )

                response = (
                    manager.list_threads(
                        cwd=(
                            repo[
                                "root"
                            ]
                            if repo.get(
                                "git"
                            )
                            else cwd
                        ),
                        limit=20,
                        include_internal=False,
                    )
                )

                _print_threads(
                    response,
                    title="=== CX HISTORY: CURRENT REPO ===",
                )

                return True

            if folded == "/history all":

                manager = _manager(
                    runtime
                )

                response = (
                    manager.list_threads(
                        limit=20,
                        include_internal=False,
                    )
                )

                _print_threads(
                    response,
                    title="=== CX HISTORY: ALL ===",
                )

                return True

            if folded in (
                "/history internal",
                "/history --internal",
            ):

                manager = _manager(
                    runtime
                )

                response = (
                    manager.list_threads(
                        limit=20,
                        internal_only=True,
                    )
                )

                _print_threads(
                    response,
                    title="=== CX HISTORY: INTERNAL / CANARY ===",
                    show_classification=True,
                )

                return True

            if folded in (
                "/history all-internal",
                "/history --all",
                "/history all --all",
                "/history all --internal",
                "/history internal-all",
            ):

                manager = _manager(
                    runtime
                )

                response = (
                    manager.list_threads(
                        limit=20,
                        include_internal=True,
                    )
                )

                _print_threads(
                    response,
                    title="=== CX HISTORY: ALL (INCLUDING INTERNAL) ===",
                    show_classification=True,
                )

                return True

            if folded == "/history archived":

                manager = _manager(
                    runtime
                )

                response = (
                    manager.list_threads(
                        cwd=(
                            repo[
                                "root"
                            ]
                            if repo.get(
                                "git"
                            )
                            else cwd
                        ),
                        limit=20,
                        archived=True,
                        include_internal=False,
                    )
                )

                _print_threads(
                    response,
                    title="=== CX HISTORY: CURRENT REPO / ARCHIVED ===",
                )

                return True

            if folded == "/history archived-all":

                manager = _manager(
                    runtime
                )

                response = (
                    manager.list_threads(
                        limit=20,
                        archived=True,
                        include_internal=False,
                    )
                )

                _print_threads(
                    response,
                    title="=== CX HISTORY: ALL / ARCHIVED ===",
                )

                return True

            if folded in (
                "/history archived-internal",
                "/history archived --internal",
            ):

                manager = _manager(
                    runtime
                )

                response = (
                    manager.list_threads(
                        limit=20,
                        archived=True,
                        internal_only=True,
                    )
                )

                _print_threads(
                    response,
                    title="=== CX HISTORY: ARCHIVED / INTERNAL ===",
                    show_classification=True,
                )

                return True

            if folded in (
                "/history archived-all-internal",
                "/history archived-all --internal",
                "/history archived-all --all",
            ):

                manager = _manager(
                    runtime
                )

                response = (
                    manager.list_threads(
                        limit=20,
                        archived=True,
                        include_internal=True,
                    )
                )

                _print_threads(
                    response,
                    title="=== CX HISTORY: ALL ARCHIVED (INCLUDING INTERNAL) ===",
                    show_classification=True,
                )

                return True

            _usage(
                "Kullanım: /history [all|internal|all-internal|archived|archived-all|archived-internal]"
            )

            return True

        # =====================================================
        # /search
        # =====================================================

        if command == "/search":

            raw_query = text[
                len(
                    "/search"
                ):
            ].strip()

            if not raw_query:

                _usage(
                    "Kullanım: /search <metin> [--all|--internal]"
                )

                return True

            include_internal = False
            internal_only = False
            query = raw_query

            if (
                raw_query.endswith(" --all")
                or raw_query.endswith(" --internal")
                or raw_query.endswith(" all-internal")
            ):
                include_internal = True
                query = raw_query.rsplit(maxsplit=1)[0].strip()
            elif raw_query.endswith(" internal") or raw_query.endswith(" --internal-only"):
                internal_only = True
                query = raw_query.rsplit(maxsplit=1)[0].strip()

            if not query:
                _usage(
                    "Kullanım: /search <metin> [--all|--internal]"
                )
                return True

            manager = _manager(
                runtime
            )

            response = (
                manager.search_threads(
                    query,
                    limit=20,
                    include_internal=include_internal,
                    internal_only=internal_only,
                )
            )

            tag = (
                " (ALL)"
                if include_internal
                else (
                    " (INTERNAL)"
                    if internal_only
                    else ""
                )
            )

            _print_threads(
                response,
                title=(
                    "=== CX HISTORY SEARCH: "
                    + query
                    + tag
                    + " ==="
                ),
                show_classification=(include_internal or internal_only),
                source="search",
            )

            return True

        # =====================================================
        # /thread
        # =====================================================

        if command == "/thread":

            argument = text[
                len(
                    "/thread"
                ):
            ].strip()

            thread_id = (
                _resolve_read_thread_id(
                    argument,
                    db,
                    repo,
                )
            )

            manager = _manager(
                runtime
            )

            response = manager.read_thread(
                thread_id,
                include_turns=False,
            )

            _print_thread(
                response[
                    "thread"
                ]
            )

            return True

        # =====================================================
        # /turns
        # =====================================================

        if command == "/turns":

            argument = text[
                len(
                    "/turns"
                ):
            ].strip()

            thread_id = (
                _resolve_read_thread_id(
                    argument,
                    db,
                    repo,
                )
            )

            manager = _manager(
                runtime
            )

            response = manager.list_turns(
                thread_id,
                limit=20,
            )

            _print_turns(
                response,
                thread_id=thread_id,
            )

            return True

        # =====================================================
        # /resume
        # =====================================================

        if command == "/resume":

            raw_ref = text[
                len(
                    "/resume"
                ):
            ].strip()

            if not raw_ref:

                _usage(
                    "Kullanım: /resume <id|no>"
                )

                return True

            thread_id = (
                ACTIVE_SELECTION_CONTEXT.resolve(
                    raw_ref
                )
            )

            if not repo.get(
                "git"
            ):

                raise HistoryManagerError(
                    "/resume repo binding için Git repo gerektiriyor."
                )

            manager = _manager(
                runtime
            )

            response = manager.read_thread(
                thread_id,
                include_turns=False,
            )

            thread = response[
                "thread"
            ]

            thread_cwd = str(
                thread.get(
                    "cwd"
                )
                or ""
            ).strip()

            if not thread_cwd:

                raise HistoryManagerError(
                    "Thread cwd yok; repo binding reddedildi."
                )

            if not _same_path(
                thread_cwd,
                repo[
                    "root"
                ],
            ):

                raise HistoryManagerError(
                    "Thread farklı bir repo'ya ait; "
                    "mevcut repo session'ına bağlanmadı."
                )

            selected = bind_repo_session(
                db,
                repo,
                thread_id,
            )

            print(
                "[cx] History thread selected."
            )

            print(
                "Thread :",
                selected[
                    "thread_id"
                ],
            )

            print(
                "Repo   :",
                selected[
                    "repo_root"
                ],
            )

            print(
                "[cx] Sonraki normal prompt bu thread'i native resume edecek."
            )

            return True

        # =====================================================
        # /rename
        # =====================================================

        if command == "/rename":

            parts = text.split(
                maxsplit=2
            )

            if len(
                parts
            ) != 3:

                _usage(
                    "Kullanım: /rename <id|no> <yeni-ad>"
                )

                return True

            raw_ref = parts[
                1
            ].strip()

            new_name = parts[
                2
            ].strip()

            thread_id = (
                ACTIVE_SELECTION_CONTEXT.resolve(
                    raw_ref
                )
            )

            manager = _manager(
                runtime
            )

            manager.rename_thread(
                thread_id,
                new_name,
            )

            ACTIVE_SELECTION_CONTEXT.update_title_for_thread(
                thread_id,
                new_name,
            )

            print(
                "[cx] Thread renamed:",
                thread_id,
            )

            print(
                "Name   :",
                new_name,
            )

            return True

        # =====================================================
        # /archive
        # =====================================================

        if command == "/archive":

            raw_ref = text[
                len(
                    "/archive"
                ):
            ].strip()

            if not raw_ref:

                _usage(
                    "Kullanım: /archive <id|no>"
                )

                return True

            thread_id = (
                ACTIVE_SELECTION_CONTEXT.resolve(
                    raw_ref
                )
            )

            manager = _manager(
                runtime
            )

            manager.archive_thread(
                thread_id
            )

            unbound = _unbind_if_selected(
                db,
                repo,
                thread_id,
            )

            ACTIVE_SELECTION_CONTEXT.clear()

            print(
                "[cx] Thread archived:",
                thread_id,
            )

            if unbound:

                print(
                    "[cx] Aktif repo session binding temizlendi."
                )

            return True

        # =====================================================
        # /unarchive
        # =====================================================

        if command == "/unarchive":

            raw_ref = text[
                len(
                    "/unarchive"
                ):
            ].strip()

            if not raw_ref:

                _usage(
                    "Kullanım: /unarchive <id|no>"
                )

                return True

            thread_id = (
                ACTIVE_SELECTION_CONTEXT.resolve(
                    raw_ref
                )
            )

            manager = _manager(
                runtime
            )

            manager.unarchive_thread(
                thread_id
            )

            ACTIVE_SELECTION_CONTEXT.clear()

            print(
                "[cx] Thread unarchived:",
                thread_id,
            )

            return True

        # =====================================================
        # /delete
        # =====================================================

        if command == "/delete":

            raw_ref = text[
                len(
                    "/delete"
                ):
            ].strip()

            if not raw_ref:

                _usage(
                    "Kullanım: /delete <id|no>"
                )

                return True

            thread_id = (
                ACTIVE_SELECTION_CONTEXT.resolve(
                    raw_ref
                )
            )

            # CX2_DELETE_SAFE_DEGRADE_01444_VS_01480_V1
            #
            # Fail closed before App Server startup or any destructive RPC.
            compatibility = (
                native_delete_compatibility()
            )

            if not compatibility[
                "supported"
            ]:

                print(
                    "[cx] Native thread delete kullanılamıyor."
                )

                print(
                    "[cx] reason="
                    + str(
                        compatibility[
                            "reason"
                        ]
                    )
                )

                print(
                    "[cx] CX pinned="
                    + str(
                        compatibility[
                            "binary_version"
                        ]
                    )
                )

                print(
                    "[cx] Bu Codex state şemasında /delete fail-closed."
                )

                print(
                    "[cx] Thread'i kaldırmak için /archive "
                    + thread_id
                    + " kullan."
                )

                return True

            manager = _manager(
                runtime
            )

            # Validate existence before asking for destructive confirmation.
            manager.read_thread(
                thread_id,
                include_turns=False,
            )

            if not _confirm_delete(
                thread_id
            ):

                return True

            manager.delete_thread(
                thread_id
            )

            unbound = _unbind_if_selected(
                db,
                repo,
                thread_id,
            )

            ACTIVE_SELECTION_CONTEXT.clear()

            print(
                "[cx] Thread permanently deleted:",
                thread_id,
            )

            if unbound:

                print(
                    "[cx] Aktif repo session binding temizlendi."
                )

            return True

        return True

    except HistoryManagerError as exc:

        print(
            "[cx] History error:",
            exc,
        )

        return True

    except Exception as exc:

        # History command failure must not terminate the interactive shell.
        print(
            "[cx] History command failed:",
            type(
                exc
            ).__name__
            + ": "
            + str(
                exc
            ),
        )

        return True