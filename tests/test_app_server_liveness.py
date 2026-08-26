from __future__ import annotations

import io
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

sys.path.insert(0, _bootstrap.RUNTIME_DIR)

from client import AppServerProtocolError
import cx2_cli
from cx2_runtime import CX2ExecutionResult, CX2Runtime
from turn_runner import StreamingTurnRunner, TurnRunResult


def scoped_notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    body = {
        "threadId": "thread-test-1",
        "turnId": "turn-test-1",
    }
    body.update(params)
    return {"method": method, "params": body}


class FakeProcess:
    """Mock subprocess.Popen object for testing process liveness."""

    def __init__(self, exit_codes: list[int | None] | None = None) -> None:
        self.exit_codes = list(exit_codes or [None])
        self.poll_count = 0

    def poll(self) -> int | None:
        self.poll_count += 1
        if len(self.exit_codes) > 1:
            return self.exit_codes.pop(0)
        return self.exit_codes[0]


class FakeLivenessClient:
    """Mock client for testing StreamingTurnRunner.wait_for_turn liveness."""

    def __init__(
        self,
        *,
        process: Any = None,
        dispatcher_thread: Any = None,
        notifications: list[dict[str, Any]] | None = None,
        server_requests: list[dict[str, Any]] | None = None,
    ) -> None:
        self.process = process
        self._dispatcher_thread = dispatcher_thread
        self.notifications = list(notifications or [])
        self.server_requests = list(server_requests or [])
        self.interrupt_calls: list[tuple[str, str]] = []

    def drain_server_requests(self) -> list[dict[str, Any]]:
        reqs = list(self.server_requests)
        self.server_requests.clear()
        return reqs

    def drain_notifications(self) -> list[dict[str, Any]]:
        notes = list(self.notifications)
        self.notifications.clear()
        return notes

    def drain_unknown(self) -> list[dict[str, Any]]:
        return []

    def request(self, method: str, params: Any = None, timeout: float = 15.0) -> Any:
        if method == "turn/interrupt":
            self.interrupt_calls.append((params.get("threadId"), params.get("turnId")))
            return {"status": "ok"}
        return {}

    def respond(self, request_id: Any, result: Any) -> None:
        pass

    def respond_error(self, request_id: Any, code: int, message: str) -> None:
        pass


class TestAppServerLiveness(unittest.TestCase):

    def setUp(self) -> None:
        self.result = TurnRunResult(
            thread_id="thread-test-1",
            turn_id="turn-test-1",
            status="inProgress",
        )

    # -------------------------------------------------------------
    # 1. Dead process detected during inProgress turn
    # -------------------------------------------------------------
    def test_dead_process_detected_during_in_progress_turn(self) -> None:
        """App Server dying mid-turn must raise AppServerProtocolError immediately without timeout wait."""
        fake_proc = FakeProcess(exit_codes=[None, 1])  # alive first check, dead second check
        client = FakeLivenessClient(process=fake_proc)
        runner = StreamingTurnRunner(client, live=False, poll_interval=0.005)

        with self.assertRaises(AppServerProtocolError) as ctx:
            runner.wait_for_turn(self.result, timeout=300.0)

        self.assertIn("terminated unexpectedly", str(ctx.exception))
        self.assertIn("exit code: 1", str(ctx.exception))
        self.assertEqual(self.result.status, "failed")
        # Ensure interrupt was NOT called against dead process
        self.assertEqual(len(client.interrupt_calls), 0)

    # -------------------------------------------------------------
    # 2. Dead process detected before wait starts
    # -------------------------------------------------------------
    def test_dead_process_detected_before_wait_starts(self) -> None:
        """If App Server is already dead before wait_for_turn starts, fail immediately."""
        fake_proc = FakeProcess(exit_codes=[137])
        client = FakeLivenessClient(process=fake_proc)
        runner = StreamingTurnRunner(client, live=False, poll_interval=0.005)

        with self.assertRaises(AppServerProtocolError) as ctx:
            runner.wait_for_turn(self.result, timeout=600.0)

        self.assertIn("exit code: 137", str(ctx.exception))
        self.assertEqual(len(client.interrupt_calls), 0)

    # -------------------------------------------------------------
    # 3. Healthy process does not false-positive
    # -------------------------------------------------------------
    def test_healthy_process_does_not_false_positive(self) -> None:
        """Healthy running process completes turn normally when turn/completed notification arrives."""
        fake_proc = FakeProcess(exit_codes=[None])
        completion_event = scoped_notification(
            "turn/completed",
            {"turn": {"id": "turn-test-1", "status": "completed"}},
        )
        client = FakeLivenessClient(process=fake_proc, notifications=[completion_event])
        runner = StreamingTurnRunner(client, live=False, poll_interval=0.005)

        res = runner.wait_for_turn(self.result, timeout=300.0)
        self.assertEqual(res.status, "completed")

    # -------------------------------------------------------------
    # 4. Terminal completion wins race over process exit
    # -------------------------------------------------------------
    def test_terminal_completion_wins_race_over_process_exit(self) -> None:
        """If turn/completed arrives and process exits right after, terminal result wins."""
        fake_proc = FakeProcess(exit_codes=[0])  # process exited with 0
        completion_event = scoped_notification(
            "turn/completed",
            {"turn": {"id": "turn-test-1", "status": "completed"}},
        )
        client = FakeLivenessClient(process=fake_proc, notifications=[completion_event])
        runner = StreamingTurnRunner(client, live=False, poll_interval=0.005)

        res = runner.wait_for_turn(self.result, timeout=300.0)
        self.assertEqual(res.status, "completed")

    # -------------------------------------------------------------
    # 5. In-flight final event race: process dead before dispatcher drains pipe
    # -------------------------------------------------------------
    def test_final_event_in_flight_when_process_exits_wins(self) -> None:
        """If process exited but dispatcher thread has buffered turn/completed, final event must win."""
        fake_proc = FakeProcess(exit_codes=[0])  # process has exited

        completion_event = scoped_notification(
            "turn/completed",
            {"turn": {"id": "turn-test-1", "status": "completed"}},
        )

        # Mock a dispatcher thread that delivers the completion event upon join()
        client = FakeLivenessClient(process=fake_proc)

        class MockInFlightDispatcher:
            def __init__(self, target_client: FakeLivenessClient, event: dict):
                self.target_client = target_client
                self.event = event
                self.alive = True

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout: float | None = None) -> None:
                # Simulate reading final bytes from pipe and enqueuing
                self.target_client.notifications.append(self.event)
                self.alive = False

        dispatcher = MockInFlightDispatcher(client, completion_event)
        client._dispatcher_thread = dispatcher
        runner = StreamingTurnRunner(client, live=False, poll_interval=0.005)

        # Must NOT raise AppServerProtocolError; terminal completion must win!
        res = runner.wait_for_turn(self.result, timeout=10.0)
        self.assertEqual(res.status, "completed")

    # -------------------------------------------------------------
    # 6. Process exits with code 0 without turn/completed -> still fails
    # -------------------------------------------------------------
    def test_process_exits_with_zero_but_no_final_event_fails(self) -> None:
        """Process exit code 0 without turn/completed must still raise AppServerProtocolError."""
        fake_proc = FakeProcess(exit_codes=[0])
        client = FakeLivenessClient(process=fake_proc)
        runner = StreamingTurnRunner(client, live=False, poll_interval=0.005)

        with self.assertRaises(AppServerProtocolError) as ctx:
            runner.wait_for_turn(self.result, timeout=10.0)

        self.assertIn("exit code: 0", str(ctx.exception))
        self.assertEqual(self.result.status, "failed")

    # -------------------------------------------------------------
    # 7. Dispatcher thread died while process still running
    # -------------------------------------------------------------
    def test_dispatcher_thread_died_while_process_alive(self) -> None:
        """If dispatcher thread terminates while process is alive and turn is inProgress, fail immediately."""
        fake_proc = FakeProcess(exit_codes=[None])  # process still alive
        client = FakeLivenessClient(process=fake_proc)

        class DeadDispatcher:
            def is_alive(self) -> bool:
                return False
            def join(self, timeout: float | None = None) -> None:
                pass

        client._dispatcher_thread = DeadDispatcher()
        runner = StreamingTurnRunner(client, live=False, poll_interval=0.005)

        with self.assertRaises(AppServerProtocolError) as ctx:
            runner.wait_for_turn(self.result, timeout=10.0)

        self.assertIn("dispatcher thread terminated unexpectedly", str(ctx.exception))
        self.assertEqual(self.result.status, "failed")

    # -------------------------------------------------------------
    # 8. Missing process handle is backward-safe
    # -------------------------------------------------------------
    def test_missing_process_handle_backward_safe(self) -> None:
        """Test client without process attribute completes normally without AttributeError."""
        completion_event = scoped_notification(
            "turn/completed",
            {"turn": {"id": "turn-test-1", "status": "completed"}},
        )
        client = FakeLivenessClient(process=None, notifications=[completion_event])
        runner = StreamingTurnRunner(client, live=False, poll_interval=0.005)

        res = runner.wait_for_turn(self.result, timeout=300.0)
        self.assertEqual(res.status, "completed")

    # -------------------------------------------------------------
    # 9. Poll probe failure handled safely
    # -------------------------------------------------------------
    def test_poll_probe_failure_handled_safely(self) -> None:
        """If process.poll() raises an unexpected exception, it is caught safely and turn completes."""
        broken_proc = MagicMock()
        broken_proc.poll.side_effect = OSError("Access denied probing process")

        completion_event = scoped_notification(
            "turn/completed",
            {"turn": {"id": "turn-test-1", "status": "completed"}},
        )
        client = FakeLivenessClient(process=broken_proc, notifications=[completion_event])
        runner = StreamingTurnRunner(client, live=False, poll_interval=0.005)

        res = runner.wait_for_turn(self.result, timeout=300.0)
        self.assertEqual(res.status, "completed")

    # -------------------------------------------------------------
    # 10. Partial command ledger preserved on death
    # -------------------------------------------------------------
    def test_partial_command_ledger_preserved_on_death(self) -> None:
        """Commands completed before App Server death remain in TurnRunResult."""
        cmd_started = scoped_notification(
            "item/started",
            {
                "item": {
                    "id": "cmd-item-1",
                    "type": "commandExecution",
                    "command": "git status",
                }
            },
        )
        cmd_completed = scoped_notification(
            "item/completed",
            {
                "item": {
                    "id": "cmd-item-1",
                    "type": "commandExecution",
                    "command": "git status",
                    "exitCode": 0,
                    "durationMs": 50,
                }
            },
        )
        fake_proc = FakeProcess(exit_codes=[None, 1])
        client = FakeLivenessClient(process=fake_proc, notifications=[cmd_started, cmd_completed])
        runner = StreamingTurnRunner(client, live=False, poll_interval=0.005)

        with self.assertRaises(AppServerProtocolError):
            runner.wait_for_turn(self.result, timeout=300.0)

        self.assertEqual(len(self.result.command_executions), 1)
        self.assertEqual(self.result.command_executions[0]["command"], "git status")
        self.assertEqual(self.result.status, "failed")

    # -------------------------------------------------------------
    # 11. Interactive Phase 1 recovery after detected death
    # -------------------------------------------------------------
    def test_interactive_phase1_recovery_after_detected_death(self) -> None:
        """Interactive loop catches death as AppServerProtocolError, closes runtime, and recovers on prompt 2."""
        class MockRuntime:
            def __init__(self):
                self.turns = 0
                self.closed_count = 0
            def start(self): pass
            def close(self):
                self.closed_count += 1
            def reset_memory_session(self): pass
            def execute_prompt(self, *, prompt, cwd, repo, db, input_items=None, quota_override=None):
                self.turns += 1
                if self.turns == 1:
                    raise AppServerProtocolError("Codex App Server process terminated unexpectedly (exit code: 1).")
                return CX2ExecutionResult(
                    blocked=False,
                    thread_id="thread-rec-1",
                    session_mode="NEW",
                    plan={"blocked": False},
                    quota={},
                    final_result=None,
                    raw_turn_result=None,
                    attempts_used=1,
                    escalations=0,
                )

        mock_rt = MockRuntime()
        inputs = ["failing prompt", "recovered prompt", "/exit"]
        out = io.StringIO()
        db = sqlite3.connect(":memory:")
        repo = {"root": str(REPO_ROOT), "git": True, "stacks": ["python"]}

        with patch("builtins.input", side_effect=inputs), \
             patch("sys.stdout", out), \
             patch("cx2_cli.CX2Runtime", return_value=mock_rt):
            exit_code = cx2_cli.interactive_loop(cwd=REPO_ROOT, repo=repo, db=db)

        db.close()
        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_rt.turns, 2)
        self.assertGreaterEqual(mock_rt.closed_count, 1)
        output = out.getvalue()
        self.assertIn("bağlantısı koptu", output.lower())
        self.assertNotIn("Traceback (most recent call last)", output)

    # -------------------------------------------------------------
    # 12. One-shot death returns nonzero cleanly
    # -------------------------------------------------------------
    def test_one_shot_death_returns_nonzero_cleanly(self) -> None:
        """One-shot execution on App Server death returns exit code 1 with clean stderr message."""
        class MockDeadRuntime:
            def __init__(self):
                self.closed = False
            def close(self):
                self.closed = True
            def execute_prompt(self, **kwargs):
                raise AppServerProtocolError("Codex App Server process terminated unexpectedly (exit code: 137).")

        mock_rt = MockDeadRuntime()
        err_out = io.StringIO()
        db = sqlite3.connect(":memory:")
        repo = {"root": str(REPO_ROOT), "git": True, "stacks": ["python"]}

        with patch("sys.stderr", err_out), \
             patch("cx2_cli.CX2Runtime", return_value=mock_rt):
            code = cx2_cli.execute_one_shot("one-shot prompt", cwd=REPO_ROOT, repo=repo, db=db)

        db.close()
        self.assertEqual(code, 1)
        self.assertTrue(mock_rt.closed)
        err_text = err_out.getvalue()
        self.assertIn("bağlantı hatası", err_text.lower())
        self.assertNotIn("Traceback (most recent call last)", err_text)


    # -------------------------------------------------------------
    # 13. Delayed dispatcher within 1.0s grace window wins
    # -------------------------------------------------------------
    def test_delayed_dispatcher_within_grace_wins(self) -> None:
        """If dispatcher thread delivers final event within 1.0s grace, terminal result wins."""
        fake_proc = FakeProcess(exit_codes=[0])
        completion_event = scoped_notification(
            "turn/completed",
            {"turn": {"id": "turn-test-1", "status": "completed"}},
        )
        client = FakeLivenessClient(process=fake_proc)

        class DelayedDispatcher(threading.Thread):
            def __init__(self, target_client: FakeLivenessClient, event: dict):
                super().__init__(daemon=True)
                self.target_client = target_client
                self.event = event

            def run(self) -> None:
                time.sleep(0.05)
                self.target_client.notifications.append(self.event)

        disp = DelayedDispatcher(client, completion_event)
        disp.start()
        client._dispatcher_thread = disp

        runner = StreamingTurnRunner(client, live=False, poll_interval=0.005)
        res = runner.wait_for_turn(self.result, timeout=10.0)
        self.assertEqual(res.status, "completed")

    # -------------------------------------------------------------
    # 14. Delayed dispatcher exceeding 1.0s grace fails with explicit diagnostic
    # -------------------------------------------------------------
    def test_delayed_dispatcher_exceeding_grace_fails_with_grace_message(self) -> None:
        """If dispatcher is still alive after 1.0s join, fail with grace exhaustion diagnostic."""
        fake_proc = FakeProcess(exit_codes=[0])
        client = FakeLivenessClient(process=fake_proc)

        class StuckDispatcher(threading.Thread):
            def __init__(self):
                super().__init__(daemon=True)
                self.stop_event = threading.Event()

            def run(self) -> None:
                # Blocks indefinitely until stopped
                self.stop_event.wait(timeout=5.0)

        disp = StuckDispatcher()
        disp.start()
        client._dispatcher_thread = disp

        runner = StreamingTurnRunner(client, live=False, poll_interval=0.005)

        with self.assertRaises(AppServerProtocolError) as ctx:
            runner.wait_for_turn(self.result, timeout=10.0)

        disp.stop_event.set()
        self.assertIn("failed to quiesce within 1.0s grace", str(ctx.exception))
        self.assertEqual(self.result.status, "failed")


if __name__ == "__main__":
    unittest.main()

