from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import sqlite3
import threading
import uuid
from typing import Any


SCHEMA_VERSION = 1
FLUSH_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_COMPLETED_RESPONSES = 200
MAX_LOGICAL_BYTES = 64 * 1024 * 1024
MAX_AGE_DAYS = 30
MAX_PENDING_FLUSHES = 1
TRANSCRIPT_FLUSH_WAIT_SECONDS = 5.0

TERMINAL_STATES = {
    "COMPLETED",
    "FAILED",
    "BLOCKED",
    "INTERRUPTED",
    "IDLE_TIMEOUT",
    "HARD_TIMEOUT",
    "PARTIAL",
    "TRUNCATED",
}


def default_transcript_path() -> Path:
    from cx_home import resolve_cx_home
    return resolve_cx_home() / "data" / "visible-transcript.sqlite3"


class TranscriptStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredResponse:
    response_id: str
    thread_id: str
    turn_id: str
    workspace_key: str
    state: str
    phase: str
    text: str
    retained_bytes: int
    total_bytes: int
    dropped_bytes: int
    line_count: int
    truncated: bool
    authoritative_source: str | None
    final_item_id: str | None
    reconciliation: list[dict[str, Any]]
    created_at: str
    updated_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _bounded_utf8_prefix(value: str, limit: int) -> tuple[str, int]:
    if limit <= 0 or not value:
        return "", 0
    parts: list[str] = []
    total = 0
    chunk_chars = 64 * 1024
    for offset in range(0, len(value), chunk_chars):
        chunk = value[offset : offset + chunk_chars]
        encoded = chunk.encode("utf-8")
        if total + len(encoded) <= limit:
            parts.append(chunk)
            total += len(encoded)
            continue
        bounded = encoded[: max(0, limit - total)]
        while bounded:
            try:
                parts.append(bounded.decode("utf-8"))
                total += len(bounded)
                break
            except UnicodeDecodeError as exc:
                bounded = bounded[: exc.start]
        return "".join(parts), total
    return value, total


def _utf8_length(value: str) -> int:
    total = 0
    chunk_chars = 64 * 1024
    for offset in range(0, len(value), chunk_chars):
        total += len(value[offset : offset + chunk_chars].encode("utf-8"))
    return total


class TranscriptStore:
    """Durable, bounded storage for user-visible canonical responses only."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_transcript_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._closed = False
        try:
            self._db = sqlite3.connect(
                self.path,
                timeout=5.0,
                isolation_level="",
                check_same_thread=False,
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._initialize()
        except Exception as exc:
            try:
                if self._db is not None:
                    self._db.close()
            except Exception:
                pass
            raise TranscriptStoreError(f"transcript database could not be opened: {exc}") from exc

    def _initialize(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS responses (
                    response_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    final_item_id TEXT,
                    workspace_key TEXT NOT NULL,
                    display_workspace TEXT NOT NULL,
                    state TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    retained_bytes INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    dropped_bytes INTEGER NOT NULL DEFAULT 0,
                    line_count INTEGER NOT NULL DEFAULT 0,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    authoritative_source TEXT,
                    reconciliation_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS responses_turn_uq
                    ON responses(thread_id, turn_id);
                CREATE INDEX IF NOT EXISTS responses_scope_idx
                    ON responses(workspace_key, thread_id, updated_at DESC, response_id DESC);
                CREATE TABLE IF NOT EXISTS response_chunks (
                    response_id TEXT NOT NULL REFERENCES responses(response_id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    utf8_bytes INTEGER NOT NULL,
                    PRIMARY KEY(response_id, chunk_index)
                );
                """
            )
            current = self._db.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if current is None:
                self._db.execute(
                    "INSERT INTO schema_metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif current["value"] != str(SCHEMA_VERSION):
                raise TranscriptStoreError(
                    f"unsupported transcript schema version {current['value']!r}"
                )
            self._db.execute("PRAGMA foreign_keys=ON")
            now = _utc_now()
            self._db.execute(
                """
                UPDATE responses
                   SET state = CASE WHEN retained_bytes > 0 THEN 'PARTIAL' ELSE 'INTERRUPTED' END,
                       phase = 'RECOVERED_STALE_ACTIVE', updated_at = ?
                 WHERE state = 'ACTIVE'
                """,
                (now,),
            )
        self.prune()

    def close(self) -> None:
        with self._lock:
            if not self._closed and self._db is not None:
                self._db.close()
                self._closed = True

    def quick_check(self) -> str:
        with self._lock:
            row = self._db.execute("PRAGMA quick_check").fetchone()
            return str(row[0]) if row else "missing"

    def start_response(
        self,
        *,
        thread_id: str,
        turn_id: str,
        workspace_key: str,
        display_workspace: str,
    ) -> "TranscriptResponseSink":
        response_id = uuid.uuid4().hex
        now = _utc_now()
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT response_id FROM responses WHERE thread_id=? AND turn_id=?",
                (thread_id, turn_id),
            ).fetchone()
            if existing is not None:
                response_id = str(existing["response_id"])
                self._db.execute(
                    "DELETE FROM response_chunks WHERE response_id=?", (response_id,)
                )
                self._db.execute(
                    """UPDATE responses SET state='ACTIVE', phase='STREAMING', updated_at=?,
                       retained_bytes=0,total_bytes=0,dropped_bytes=0,line_count=0,truncated=0,
                       final_item_id=NULL,authoritative_source=NULL,reconciliation_json='[]'
                       WHERE response_id=?""",
                    (now, response_id),
                )
            else:
                self._db.execute(
                    """INSERT INTO responses(
                       response_id,thread_id,turn_id,workspace_key,display_workspace,
                       state,phase,created_at,updated_at)
                       VALUES(?,?,?,?,?,'ACTIVE','STREAMING',?,?)""",
                    (
                        response_id,
                        thread_id,
                        turn_id,
                        workspace_key,
                        display_workspace,
                        now,
                        now,
                    ),
                )
        return TranscriptResponseSink(self, response_id)

    def _append_chunk(
        self,
        response_id: str,
        text: str,
        *,
        observed_bytes: int,
    ) -> tuple[int, int]:
        if not text and observed_bytes <= 0:
            return 0, 0
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT retained_bytes,total_bytes FROM responses WHERE response_id=? AND state='ACTIVE'",
                (response_id,),
            ).fetchone()
            if row is None:
                raise TranscriptStoreError("response is no longer active")
            retained = int(row["retained_bytes"])
            remaining = max(0, MAX_RESPONSE_BYTES - retained)
            accepted, accepted_bytes = _bounded_utf8_prefix(text, remaining)
            if accepted:
                index_row = self._db.execute(
                    "SELECT COALESCE(MAX(chunk_index), -1) + 1 FROM response_chunks WHERE response_id=?",
                    (response_id,),
                ).fetchone()
                self._db.execute(
                    "INSERT INTO response_chunks(response_id,chunk_index,text,utf8_bytes) VALUES(?,?,?,?)",
                    (response_id, int(index_row[0]), accepted, accepted_bytes),
                )
            new_total = int(row["total_bytes"]) + max(0, observed_bytes)
            new_retained = retained + accepted_bytes
            self._db.execute(
                """UPDATE responses SET retained_bytes=?,total_bytes=?,dropped_bytes=?,
                   truncated=?,updated_at=? WHERE response_id=?""",
                (
                    new_retained,
                    new_total,
                    max(0, new_total - new_retained),
                    int(new_total > new_retained),
                    _utc_now(),
                    response_id,
                ),
            )
        return accepted_bytes, max(0, observed_bytes - accepted_bytes)

    def _finalize(
        self,
        response_id: str,
        *,
        canonical_text: str,
        state: str,
        phase: str,
        authoritative_source: str | None,
        final_item_id: str | None,
        reconciliation: list[dict[str, Any]] | None,
    ) -> None:
        normalized_state = state.upper()
        if normalized_state not in TERMINAL_STATES:
            normalized_state = "FAILED"
        retained_text, retained_bytes = _bounded_utf8_prefix(
            canonical_text, MAX_RESPONSE_BYTES
        )
        total_bytes = _utf8_length(canonical_text)
        if total_bytes > retained_bytes and normalized_state == "COMPLETED":
            normalized_state = "TRUNCATED"
        chunks: list[tuple[str, int]] = []
        encoded_retained = retained_text.encode("utf-8")
        offset = 0
        while offset < len(encoded_retained):
            end = min(len(encoded_retained), offset + FLUSH_BYTES)
            bounded = encoded_retained[offset:end]
            while bounded:
                try:
                    chunk = bounded.decode("utf-8")
                    break
                except UnicodeDecodeError as exc:
                    bounded = bounded[:exc.start]
            else:
                break
            chunks.append((chunk, len(bounded)))
            offset += len(bounded)
        line_count = (
            canonical_text.count("\n") + (0 if not canonical_text or canonical_text.endswith("\n") else 1)
        )
        safe_reconciliation = list((reconciliation or [])[:64])
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM response_chunks WHERE response_id=?", (response_id,)
            )
            for index, (chunk, chunk_bytes) in enumerate(chunks):
                self._db.execute(
                    "INSERT INTO response_chunks(response_id,chunk_index,text,utf8_bytes) VALUES(?,?,?,?)",
                    (response_id, index, chunk, chunk_bytes),
                )
            updated = self._db.execute(
                """UPDATE responses SET state=?,phase=?,updated_at=?,retained_bytes=?,
                   total_bytes=?,dropped_bytes=?,line_count=?,truncated=?,authoritative_source=?,
                   final_item_id=?,reconciliation_json=? WHERE response_id=?""",
                (
                    normalized_state,
                    phase,
                    _utc_now(),
                    retained_bytes,
                    total_bytes,
                    max(0, total_bytes - retained_bytes),
                    line_count,
                    int(total_bytes > retained_bytes),
                    authoritative_source,
                    final_item_id,
                    json.dumps(safe_reconciliation, ensure_ascii=False, separators=(",", ":")),
                    response_id,
                ),
            ).rowcount
            if not updated:
                raise TranscriptStoreError("response disappeared before finalization")
        self.prune()

    def get_last(
        self,
        *,
        workspace_key: str,
        thread_id: str | None = None,
    ) -> StoredResponse | None:
        where = "workspace_key=? AND state<>'ACTIVE'"
        args: list[Any] = [workspace_key]
        if thread_id:
            where += " AND thread_id=?"
            args.append(thread_id)
        with self._lock:
            row = self._db.execute(
                f"SELECT * FROM responses WHERE {where} ORDER BY updated_at DESC,response_id DESC LIMIT 1",
                args,
            ).fetchone()
            if row is None:
                return None
            chunks = self._db.execute(
                "SELECT text FROM response_chunks WHERE response_id=? ORDER BY chunk_index",
                (row["response_id"],),
            ).fetchall()
        try:
            reconciliation = json.loads(row["reconciliation_json"])
        except (TypeError, ValueError):
            reconciliation = []
        return StoredResponse(
            response_id=str(row["response_id"]),
            thread_id=str(row["thread_id"]),
            turn_id=str(row["turn_id"]),
            workspace_key=str(row["workspace_key"]),
            state=str(row["state"]),
            phase=str(row["phase"]),
            text="".join(str(chunk["text"]) for chunk in chunks),
            retained_bytes=int(row["retained_bytes"]),
            total_bytes=int(row["total_bytes"]),
            dropped_bytes=int(row["dropped_bytes"]),
            line_count=int(row["line_count"]),
            truncated=bool(row["truncated"]),
            authoritative_source=row["authoritative_source"],
            final_item_id=row["final_item_id"],
            reconciliation=reconciliation if isinstance(reconciliation, list) else [],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def clear_scope(self, *, workspace_key: str, thread_id: str | None = None) -> int:
        where = "workspace_key=?"
        args: list[Any] = [workspace_key]
        if thread_id:
            where += " AND thread_id=?"
            args.append(thread_id)
        with self._lock, self._db:
            count_row = self._db.execute(
                f"SELECT COUNT(*) FROM responses WHERE {where}", args
            ).fetchone()
            deleted = int(count_row[0]) if count_row else 0
            self._db.execute(f"DELETE FROM responses WHERE {where}", args)
        return deleted

    def prune(self) -> None:
        with self._lock, self._db:
            self._db.execute(
                """DELETE FROM response_chunks WHERE response_id IN (
                       SELECT response_id FROM responses
                        WHERE state<>'ACTIVE' AND julianday(updated_at) < julianday('now', ?)
                   )""",
                (f"-{MAX_AGE_DAYS} days",),
            )
            self._db.execute(
                "DELETE FROM responses WHERE state<>'ACTIVE' AND julianday(updated_at) < julianday('now', ?)",
                (f"-{MAX_AGE_DAYS} days",),
            )
            # Rank and account in SQLite so pruning does not materialize an
            # attacker-controlled response list in Python memory.
            prune_predicate = (
                "row_number > ? OR logical_bytes > ?"
            )
            ranked_sql = """
                SELECT response_id,
                       ROW_NUMBER() OVER (
                           ORDER BY updated_at DESC,response_id DESC
                       ) AS row_number,
                       SUM(retained_bytes) OVER (
                           ORDER BY updated_at DESC,response_id DESC
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS logical_bytes
                  FROM responses
                 WHERE state<>'ACTIVE'
            """
            self._db.execute(
                f"""DELETE FROM response_chunks WHERE response_id IN (
                        SELECT response_id FROM ({ranked_sql})
                         WHERE {prune_predicate}
                    )""",
                (MAX_COMPLETED_RESPONSES, MAX_LOGICAL_BYTES),
            )
            self._db.execute(
                f"""DELETE FROM responses WHERE response_id IN (
                        SELECT response_id FROM ({ranked_sql})
                         WHERE {prune_predicate}
                    )""",
                (MAX_COMPLETED_RESPONSES, MAX_LOGICAL_BYTES),
            )


class TranscriptResponseSink:
    def __init__(self, store: TranscriptStore, response_id: str) -> None:
        self.store = store
        self.response_id = response_id
        self._buffer = ""
        self._buffer_bytes = 0

        self._observed_bytes = 0
        self._closed = False
        self.truncated = False
        self._writer_error: BaseException | None = None
        self._flush_queue: queue.Queue[
            tuple[str | None, int, threading.Event]
        ] = queue.Queue(maxsize=MAX_PENDING_FLUSHES)
        self._writer = threading.Thread(
            target=self._writer_loop,
            name="cx2-transcript-writer",
            daemon=True,
        )
        self._writer.start()

    def _writer_loop(self) -> None:
        while True:
            text, observed, acknowledged = self._flush_queue.get()
            try:
                if text is None:
                    return
                if self._writer_error is None:
                    self.store._append_chunk(
                        self.response_id,
                        text,
                        observed_bytes=observed,
                    )
            except BaseException as exc:
                self._writer_error = exc
            finally:
                acknowledged.set()
                self._flush_queue.task_done()

    def _raise_writer_error(self) -> None:
        if self._writer_error is not None:
            raise TranscriptStoreError(
                "asynchronous transcript flush failed: "
                f"{type(self._writer_error).__name__}: {self._writer_error}"
            ) from self._writer_error

    def _enqueue(self, value: str, observed: int, *, wait: bool) -> None:
        self._raise_writer_error()
        acknowledged = threading.Event()
        try:
            self._flush_queue.put(
                (value, observed, acknowledged),
                timeout=TRANSCRIPT_FLUSH_WAIT_SECONDS,
            )
        except queue.Full as exc:
            raise TranscriptStoreError(
                "bounded transcript writer queue did not make progress"
            ) from exc
        if wait and not acknowledged.wait(TRANSCRIPT_FLUSH_WAIT_SECONDS):
            raise TranscriptStoreError("transcript flush acknowledgement timed out")
        if wait:
            self._raise_writer_error()

    def _flush_buffer(self, *, wait: bool) -> None:
        if self._closed:
            return
        if self._buffer:
            value = self._buffer
            observed = self._buffer_bytes
            self._buffer = ""
            self._buffer_bytes = 0
            self._enqueue(value, observed, wait=wait)
        elif wait:
            marker = threading.Event()
            try:
                self._flush_queue.put(
                    ("", 0, marker),
                    timeout=TRANSCRIPT_FLUSH_WAIT_SECONDS,
                )
            except queue.Full as exc:
                raise TranscriptStoreError(
                    "bounded transcript writer queue did not drain"
                ) from exc
            if not marker.wait(TRANSCRIPT_FLUSH_WAIT_SECONDS):
                raise TranscriptStoreError("transcript drain acknowledgement timed out")
            self._raise_writer_error()

    def append(self, value: str) -> None:
        if self._closed or not value:
            return
        remaining = value
        while remaining:
            capacity = max(1, FLUSH_BYTES - self._buffer_bytes)
            piece, piece_bytes = _bounded_utf8_prefix(remaining, capacity)
            if not piece:
                self.flush()
                continue
            self._buffer += piece
            self._buffer_bytes += piece_bytes
            self._observed_bytes += piece_bytes
            remaining = remaining[len(piece) :]
            if self._buffer_bytes >= FLUSH_BYTES:
                self._flush_buffer(wait=False)

    def flush(self) -> None:
        self._flush_buffer(wait=True)

    def _stop_writer(self) -> None:
        if not self._writer.is_alive():
            self._raise_writer_error()
            return
        acknowledged = threading.Event()
        try:
            self._flush_queue.put(
                (None, 0, acknowledged),
                timeout=TRANSCRIPT_FLUSH_WAIT_SECONDS,
            )
        except queue.Full as exc:
            raise TranscriptStoreError("transcript writer stop queue timed out") from exc
        if not acknowledged.wait(TRANSCRIPT_FLUSH_WAIT_SECONDS):
            raise TranscriptStoreError("transcript writer stop acknowledgement timed out")
        self._writer.join(timeout=TRANSCRIPT_FLUSH_WAIT_SECONDS)
        if self._writer.is_alive():
            raise TranscriptStoreError("transcript writer did not stop")
        self._raise_writer_error()

    def finalize(
        self,
        *,
        canonical_text: str,
        state: str,
        phase: str,
        authoritative_source: str | None = None,
        final_item_id: str | None = None,
        reconciliation: list[dict[str, Any]] | None = None,
    ) -> None:
        if self._closed:
            return
        self._flush_buffer(wait=True)
        self._stop_writer()
        # Canonical finalization transactionally replaces all provisional chunks.
        self.truncated = _utf8_length(canonical_text) > MAX_RESPONSE_BYTES
        self.store._finalize(
            self.response_id,
            canonical_text=canonical_text,
            state=state,
            phase=phase,
            authoritative_source=authoritative_source,
            final_item_id=final_item_id,
            reconciliation=reconciliation,
        )
        self._closed = True
        self._buffer = ""
        self._buffer_bytes = 0

    def abort(self) -> None:
        """Boundedly stop a failed sink without altering the turn outcome."""

        if self._closed:
            return
        self._closed = True
        self._buffer = ""
        self._buffer_bytes = 0
        try:
            self._stop_writer()
        except Exception:
            pass


__all__ = [
    "FLUSH_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_COMPLETED_RESPONSES",
    "MAX_LOGICAL_BYTES",
    "MAX_AGE_DAYS",
    "MAX_PENDING_FLUSHES",
    "StoredResponse",
    "TranscriptResponseSink",
    "TranscriptStore",
    "TranscriptStoreError",
    "default_transcript_path",
]
