from __future__ import annotations

import sqlite3
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _bootstrap  # noqa: E402
sys.path.insert(0, str(_bootstrap.RUNTIME_DIR))

from transcript_store import (
    FLUSH_BYTES,
    MAX_AGE_DAYS,
    MAX_COMPLETED_RESPONSES,
    MAX_LOGICAL_BYTES,
    MAX_RESPONSE_BYTES,
    TranscriptStore,
    TranscriptStoreError,
)


class TestTranscriptStore(unittest.TestCase):
    def setUp(self) -> None:
        fd, name = tempfile.mkstemp(
            prefix="cx2-transcript-test-",
            suffix=".sqlite3",
            dir=_bootstrap.TEST_TEMP_ROOT,
        )
        import os
        os.close(fd)
        self.path = Path(name)
        self.path.unlink(missing_ok=True)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)

    def test_stream_flush_and_canonical_replacement(self) -> None:
        store = TranscriptStore(self.path)
        sink = store.start_response(
            thread_id="thread-1",
            turn_id="turn-1",
            workspace_key="C:/workspace",
            display_workspace="C:/workspace",
        )
        sink.append("provisional " + ("x" * FLUSH_BYTES))
        sink.flush()
        sink.finalize(
            canonical_text="authoritative ✓",
            state="COMPLETED",
            phase="CANONICAL_FINAL",
            authoritative_source="item/completed",
        )
        response = store.get_last(workspace_key="C:/workspace", thread_id="thread-1")
        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response.text, "authoritative ✓")
        self.assertEqual(response.state, "COMPLETED")
        self.assertEqual(store.quick_check(), "ok")
        store.close()

    def test_response_bound_is_utf8_safe(self) -> None:
        store = TranscriptStore(self.path)
        sink = store.start_response(
            thread_id="thread-2", turn_id="turn-2", workspace_key="w", display_workspace="w"
        )
        sink.finalize(
            canonical_text="🙂" * (MAX_RESPONSE_BYTES // 4 + 3),
            state="FAILED",
            phase="TERMINAL_NON_SUCCESS",
        )
        response = store.get_last(workspace_key="w", thread_id="thread-2")
        self.assertIsNotNone(response)
        assert response is not None
        self.assertLessEqual(len(response.text.encode("utf-8")), MAX_RESPONSE_BYTES)
        response.text.encode("utf-8")
        self.assertTrue(response.truncated)
        store.close()

    def test_stale_active_is_recovered_and_clear_is_scoped(self) -> None:
        store = TranscriptStore(self.path)
        sink = store.start_response(
            thread_id="thread-3", turn_id="turn-3", workspace_key="w", display_workspace="w"
        )
        sink.append("partial")
        sink.flush()
        store.close()

        recovered = TranscriptStore(self.path)
        row = recovered.get_last(workspace_key="w", thread_id="thread-3")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn(row.state, {"PARTIAL", "INTERRUPTED"})
        self.assertEqual(recovered.clear_scope(workspace_key="other"), 0)
        self.assertEqual(recovered.clear_scope(workspace_key="w", thread_id="thread-3"), 1)
        self.assertIsNone(recovered.get_last(workspace_key="w", thread_id="thread-3"))
        recovered.close()

    def _finalize(self, store: TranscriptStore, index: int, text: str = "x", state: str = "COMPLETED") -> None:
        sink = store.start_response(
            thread_id="thread", turn_id=f"turn-{index}", workspace_key="w", display_workspace="w"
        )
        sink.finalize(canonical_text=text, state=state, phase="FINAL")

    def test_exact_response_boundary_and_terminal_states_survive_reopen(self) -> None:
        store = TranscriptStore(self.path)
        exact = "a" * MAX_RESPONSE_BYTES
        self._finalize(store, 1, exact)
        row = store.get_last(workspace_key="w", thread_id="thread")
        assert row is not None
        self.assertEqual(row.retained_bytes, MAX_RESPONSE_BYTES)
        self.assertEqual(row.dropped_bytes, 0)
        self.assertFalse(row.truncated)
        store.close()
        reopened = TranscriptStore(self.path)
        row = reopened.get_last(workspace_key="w", thread_id="thread")
        assert row is not None
        self.assertEqual(row.text, exact)
        reopened.close()

    def test_completed_row_limit_and_active_row_exemption(self) -> None:
        store = TranscriptStore(self.path)
        active = store.start_response(
            thread_id="active", turn_id="active", workspace_key="w", display_workspace="w"
        )
        active.append("still running")
        active.flush()
        for index in range(MAX_COMPLETED_RESPONSES):
            self._finalize(store, index, "")
        self.assertEqual(
            store._db.execute("SELECT COUNT(*) FROM responses WHERE state<>'ACTIVE'").fetchone()[0],
            MAX_COMPLETED_RESPONSES,
        )
        self._finalize(store, MAX_COMPLETED_RESPONSES, "")
        completed = store._db.execute("SELECT COUNT(*) FROM responses WHERE state<>'ACTIVE'").fetchone()[0]
        active_count = store._db.execute("SELECT COUNT(*) FROM responses WHERE state='ACTIVE'").fetchone()[0]
        self.assertEqual(completed, MAX_COMPLETED_RESPONSES)
        self.assertEqual(active_count, 1)
        store.close()

    def test_logical_byte_limit_prunes_deterministic_oldest(self) -> None:
        store = TranscriptStore(self.path)
        with store._lock, store._db:
            for index, retained in enumerate([MAX_RESPONSE_BYTES] * 4):
                response_id = f"logical-{index}"
                stamp = f"2026-08-2{index}T00:00:00.000+00:00"
                store._db.execute(
                    """INSERT INTO responses(response_id,thread_id,turn_id,workspace_key,
                       display_workspace,state,phase,created_at,updated_at,retained_bytes,total_bytes)
                       VALUES(?,?,?,?,?,'COMPLETED','FINAL',?,?,?,?)""",
                    (response_id, "logical", response_id, "logical", "logical", stamp, stamp, retained, retained),
                )
        store.prune()
        self.assertEqual(
            store._db.execute("SELECT SUM(retained_bytes) FROM responses WHERE thread_id='logical'").fetchone()[0],
            MAX_LOGICAL_BYTES,
        )
        with store._lock, store._db:
            store._db.execute(
                """INSERT INTO responses(response_id,thread_id,turn_id,workspace_key,
                   display_workspace,state,phase,created_at,updated_at,retained_bytes,total_bytes)
                   VALUES(?,?,?,?,?,'COMPLETED','FINAL',?,?,?,?)""",
                ("logical-4", "logical", "logical-4", "logical", "logical",
                 "2026-08-24T00:00:00.000+00:00", "2026-08-24T00:00:00.000+00:00", 1, 1),
            )
        store.prune()
        rows = {
            row[0] for row in store._db.execute(
                "SELECT response_id FROM responses WHERE thread_id='logical'"
            ).fetchall()
        }
        self.assertEqual(len(rows), 4)
        self.assertNotIn("logical-0", rows)
        self.assertEqual(
            store._db.execute("SELECT SUM(retained_bytes) FROM responses WHERE thread_id='logical'").fetchone()[0],
            MAX_LOGICAL_BYTES - MAX_RESPONSE_BYTES + 1,
        )
        store.close()

    def test_all_terminal_states_and_reconciliation_metadata_survive_reopen(self) -> None:
        store = TranscriptStore(self.path)
        for index, state in enumerate(
            ["COMPLETED", "FAILED", "BLOCKED", "INTERRUPTED", "IDLE_TIMEOUT", "HARD_TIMEOUT", "PARTIAL"]
        ):
            sink = store.start_response(
                thread_id=f"thread-{index}", turn_id=f"turn-{index}",
                workspace_key=f"w-{index}", display_workspace=f"w-{index}",
            )
            sink.append("stale streamed")
            sink.finalize(
                canonical_text=f"canonical-{state}", state=state, phase="TERMINAL_NON_SUCCESS",
                authoritative_source="item/completed", final_item_id="final",
                reconciliation=[{"relationship": "divergent"}],
            )
        store.close()
        reopened = TranscriptStore(self.path)
        for index, state in enumerate(
            ["COMPLETED", "FAILED", "BLOCKED", "INTERRUPTED", "IDLE_TIMEOUT", "HARD_TIMEOUT", "PARTIAL"]
        ):
            row = reopened.get_last(workspace_key=f"w-{index}", thread_id=f"thread-{index}")
            self.assertIsNotNone(row)
            self.assertEqual(row.state, state)
            self.assertEqual(row.text, f"canonical-{state}")
            self.assertEqual(row.reconciliation, [{"relationship": "divergent"}])
        reopened.close()

    def test_age_pruning_keeps_recent_and_active(self) -> None:
        store = TranscriptStore(self.path)
        self._finalize(store, 1, "recent")
        active = store.start_response(
            thread_id="active", turn_id="old-active", workspace_key="w", display_workspace="w"
        )
        active.append("active")
        active.flush()
        with store._lock, store._db:
            store._db.execute(
                "UPDATE responses SET updated_at=datetime('now', ?) WHERE turn_id='turn-1'",
                (f"-{MAX_AGE_DAYS + 1} days",),
            )
            store._db.execute(
                "UPDATE responses SET updated_at=datetime('now', ?) WHERE turn_id='old-active'",
                (f"-{MAX_AGE_DAYS + 1} days",),
            )
        store.prune()
        self.assertEqual(store._db.execute("SELECT COUNT(*) FROM responses WHERE turn_id='turn-1'").fetchone()[0], 0)
        self.assertEqual(store._db.execute("SELECT COUNT(*) FROM responses WHERE turn_id='old-active'").fetchone()[0], 1)
        store.close()

    def test_corrupt_database_fails_closed(self) -> None:
        self.path.write_bytes(b"not a sqlite database")
        with self.assertRaises(TranscriptStoreError):
            TranscriptStore(self.path)


if __name__ == "__main__":
    unittest.main()
