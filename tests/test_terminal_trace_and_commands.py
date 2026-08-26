from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import gc
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _bootstrap  # noqa: E402
sys.path.insert(0, str(_bootstrap.RUNTIME_DIR))

from cx2_cli import handle_interactive_command  # noqa: E402
from cx2_runtime import CX2Runtime  # noqa: E402
from history_cli import handle_history_command  # noqa: E402
from session_adapter import canonical_cwd_key  # noqa: E402
from transcript_store import TranscriptStore  # noqa: E402


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestTraceAndTranscriptCommands(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(":memory:")

    def tearDown(self) -> None:
        self.db.close()
        # Windows can briefly retain a just-closed SQLite/WAL directory handle.
        # Retry a small bounded window, but never hide a persistent cleanup
        # failure or redirect temp state into the repository.
        for attempt in range(5):
            try:
                self.temp.cleanup()
                break
            except OSError:
                if attempt == 4:
                    raise
                gc.collect()
                time.sleep(0.05)

    def _stored_runtime(self) -> tuple[CX2Runtime, TranscriptStore]:
        runtime = CX2Runtime(live=False)
        store = TranscriptStore(self.root / "visible.sqlite3")
        runtime._transcript_store = store
        for workspace, thread, turn, text in (
            (self.root, "thread-a", "turn-a", "secret-a"),
            (self.root / "other", "thread-b", "turn-b", "secret-b"),
        ):
            sink = store.start_response(
                thread_id=thread,
                turn_id=turn,
                workspace_key=canonical_cwd_key(workspace),
                display_workspace=str(workspace),
            )
            sink.finalize(canonical_text=text, state="COMPLETED", phase="FINAL")
        return runtime, store

    def test_trace_is_64_entry_utf8_bounded_and_truthful(self) -> None:
        runtime = CX2Runtime(live=False)
        huge = "🙂" * 20000
        commands = [
            {
                "display_command": f"command-{index}" if index < 64 else huge,
                "cwd": str(self.root),
                "status": "blocked" if index == 1 else "failed",
                "exit_code": 1,
                "duration_ms": index,
                "classification_text": huge,
                "output_snippet": huge,
                "output_total_bytes": len(huge.encode("utf-8")) + 100,
                "bounded_host_execution": index == 64,
            }
            for index in range(65)
        ]
        runtime._capture_trace(SimpleNamespace(command_executions=commands))
        self.assertEqual(len(runtime.last_trace), 64)
        self.assertEqual(runtime.last_trace_dropped_entries, 1)
        last = runtime.last_trace[-1]
        self.assertLessEqual(len(last["command"].encode("utf-8")), 16 * 1024)
        self.assertGreater(last["command_dropped_bytes"], 0)
        self.assertGreater(last["classification_dropped_bytes"], 0)
        self.assertTrue(last["output_truncated"])
        self.assertTrue(last["host_execution"])

        out = io.StringIO()
        with redirect_stdout(out):
            handle_interactive_command(
                "/trace", runtime=runtime, db=self.db, cwd=self.root, repo={"git": False}
            )
        self.assertIn("bounded to 64 commands", out.getvalue())
        self.assertIn("command truncated", out.getvalue())
        self.assertIn("output truncated", out.getvalue())

    def test_new_session_clears_trace_and_grants(self) -> None:
        runtime = CX2Runtime(live=False)
        runtime.last_trace = [{"command": "old"}]
        runtime.last_trace_dropped_entries = 4
        runtime.file_write_grants.grant(thread_id="t", workspace_root=self.root)
        runtime.reset_memory_session()
        self.assertEqual(runtime.last_trace, [])
        self.assertEqual(runtime.last_trace_dropped_entries, 0)
        self.assertFalse(runtime.file_write_grants.has(thread_id="t", workspace_root=self.root))

    def test_runtime_context_preserves_same_scope_and_invalidates_changes(self) -> None:
        runtime = CX2Runtime(live=False)
        cwd_key = canonical_cwd_key(self.root)
        runtime._activate_runtime_context(thread_id="thread-a", cwd_key=cwd_key)
        runtime.file_write_grants.grant(thread_id="thread-a", workspace_root=self.root)
        runtime.last_trace = [{"command": "previous"}]
        runtime._activate_runtime_context(thread_id="thread-a", cwd_key=cwd_key)
        self.assertTrue(runtime.file_write_grants.has(thread_id="thread-a", workspace_root=self.root))
        self.assertEqual(runtime.last_trace, [{"command": "previous"}])

        runtime._activate_runtime_context(thread_id="thread-b", cwd_key=cwd_key)
        self.assertFalse(runtime.file_write_grants.has(thread_id="thread-a", workspace_root=self.root))
        self.assertEqual(runtime.last_trace, [])

        runtime.file_write_grants.grant(thread_id="thread-b", workspace_root=self.root)
        runtime._activate_runtime_context(
            thread_id="thread-b", cwd_key=canonical_cwd_key(self.root / "other")
        )
        self.assertFalse(runtime.file_write_grants.has(thread_id="thread-b", workspace_root=self.root))

    def test_last_git_context_never_falls_back_to_other_identity(self) -> None:
        runtime, store = self._stored_runtime()
        with patch(
            "cx2_runtime.evaluate_session",
            return_value={"reusable": True, "session": {"thread_id": "thread-a"}},
        ):
            row = runtime.last_visible_response(cwd=self.root, repo={"git": True}, db=self.db)
        self.assertIsNotNone(row)
        self.assertEqual(row.text, "secret-a")
        for session in (
            {"reusable": False, "session": {"thread_id": "thread-a"}},
            {"reusable": True, "session": {"thread_id": "missing"}},
            {"reusable": True, "session": None},
        ):
            with patch("cx2_runtime.evaluate_session", return_value=session):
                self.assertIsNone(
                    runtime.last_visible_response(cwd=self.root, repo={"git": True}, db=self.db)
                )
        runtime._transcript_store = None
        store.close()

    def test_last_non_git_requires_memory_identity(self) -> None:
        runtime, store = self._stored_runtime()
        self.assertIsNone(runtime.last_visible_response(cwd=self.root, repo={"git": False}, db=self.db))
        runtime.active_non_git_thread_id = "thread-a"
        row = runtime.last_visible_response(cwd=self.root, repo={"git": False}, db=self.db)
        self.assertIsNotNone(row)
        self.assertEqual(row.text, "secret-a")
        self.assertIsNone(
            runtime.last_visible_response(cwd=self.root / "other", repo={"git": False}, db=self.db)
        )
        runtime._transcript_store = None
        store.close()

    def test_transcript_clear_decline_accept_and_non_tty(self) -> None:
        runtime, store = self._stored_runtime()
        runtime.last_trace = [{"command": "preserve"}]
        with patch.object(sys, "stdin", TTY()), patch.object(sys, "stdout", TTY()), patch(
            "builtins.input", return_value="n"
        ):
            handle_interactive_command(
                "/transcript clear", runtime=runtime, db=self.db, cwd=self.root, repo={"git": False}
            )
        self.assertIsNotNone(store.get_last(workspace_key=canonical_cwd_key(self.root)))

        with patch.object(sys, "stdin", TTY()), patch.object(sys, "stdout", TTY()), patch(
            "builtins.input", return_value="y"
        ):
            handle_interactive_command(
                "/transcript clear", runtime=runtime, db=self.db, cwd=self.root, repo={"git": False}
            )
        self.assertIsNone(store.get_last(workspace_key=canonical_cwd_key(self.root)))
        self.assertIsNotNone(store.get_last(workspace_key=canonical_cwd_key(self.root / "other")))
        self.assertEqual(runtime.last_trace, [{"command": "preserve"}])

        err = io.StringIO()
        with redirect_stderr(err):
            handle_interactive_command(
                "/transcript clear", runtime=runtime, db=self.db, cwd=self.root / "other", repo={"git": False}
            )
        self.assertIn("yalnızca etkileşimli", err.getvalue())
        self.assertIsNotNone(store.get_last(workspace_key=canonical_cwd_key(self.root / "other")))
        runtime._transcript_store = None
        store.close()

    def test_quota_command_refreshes_status_snapshot(self) -> None:
        runtime = SimpleNamespace(client=object(), start=lambda: None)
        quota = {"available": True, "remainingPercent": 73}
        out = io.StringIO()
        with patch("cx2_cli.read_live_quota", return_value=quota), patch(
            "cx2_cli.production_cx.print_quota"
        ), patch("cx2_cli._CX2_TERMINAL.set_status_snapshot") as status, redirect_stdout(out):
            handled, should_exit = handle_interactive_command(
                "/quota", runtime=runtime, db=self.db, cwd=self.root, repo={"git": False}
            )
        self.assertTrue(handled)
        self.assertFalse(should_exit)
        status.assert_called_once_with(quota=quota)

    def test_resume_command_invalidates_runtime_scoped_state(self) -> None:
        runtime = SimpleNamespace(reset_memory_session=MagicMock())
        manager = SimpleNamespace(
            read_thread=lambda *_args, **_kwargs: {
                "thread": {"id": "thread-a", "cwd": str(self.root)}
            }
        )
        with patch("history_cli._manager", return_value=manager), patch(
            "history_cli.bind_repo_session",
            return_value={"thread_id": "thread-a", "repo_root": str(self.root)},
        ), redirect_stdout(io.StringIO()):
            handled = handle_history_command(
                "/resume thread-a",
                runtime=runtime,
                db=self.db,
                cwd=self.root,
                repo={"git": True, "root": str(self.root)},
            )
        self.assertTrue(handled)
        runtime.reset_memory_session.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
