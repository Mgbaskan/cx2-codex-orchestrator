from __future__ import annotations

import io
from pathlib import Path
import sqlite3
import sys
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

sys.path.insert(0, _bootstrap.RUNTIME_DIR)

import cx2_cli
from client import AppServerProtocolError
from cx2_runtime import CX2ExecutionResult, CX2Runtime, CX2RuntimeError


class FakeRuntime:
    """Hermetic stub for CX2Runtime to test interactive loop exception boundaries."""

    def __init__(self, side_effects: list[Any] | None = None) -> None:
        self.live = True
        self.interactive = True
        self.started = False
        self.initialized = False
        self.closed = False
        self.reset_memory_called = False
        self.side_effects = list(side_effects or [])
        self.calls: list[dict[str, Any]] = []

    def start(self) -> None:
        self.started = True
        self.initialized = True
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.started = False
        self.initialized = False

    def reset_memory_session(self) -> None:
        self.reset_memory_called = True

    def execute_prompt(
        self,
        *,
        prompt: str,
        cwd: Path,
        repo: dict[str, Any],
        db: Any,
        input_items: list[dict] | None = None,
        quota_override: dict[str, Any] | None = None,
    ) -> CX2ExecutionResult:
        self.calls.append({"prompt": prompt, "cwd": cwd})
        if self.side_effects:
            effect = self.side_effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            elif isinstance(effect, type) and issubclass(effect, BaseException):
                raise effect("simulated error")
            return effect

        return CX2ExecutionResult(
            blocked=False,
            thread_id="fake-thread-1",
            session_mode="NEW",
            plan={"blocked": False},
            quota={},
            final_result=None,
            raw_turn_result=None,
            attempts_used=1,
            escalations=0,
        )


class TestInteractiveFailureBoundary(unittest.TestCase):

    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.repo = {"root": str(REPO_ROOT), "git": True, "stacks": ["python"]}
        self.cwd = REPO_ROOT

    def tearDown(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    # -------------------------------------------------------------
    # A. TimeoutError containment in interactive_loop
    # -------------------------------------------------------------
    def test_interactive_timeout_error_containment(self) -> None:
        """TimeoutError during execute_prompt must NOT terminate the interactive loop."""
        fake_runtime = FakeRuntime(side_effects=[TimeoutError("turn/completed timeout")])

        inputs = ["first failing prompt", "/exit"]
        stdout_capture = io.StringIO()

        with patch("builtins.input", side_effect=inputs), \
             patch("sys.stdout", stdout_capture), \
             patch("cx2_cli.CX2Runtime", return_value=fake_runtime):
            exit_code = cx2_cli.interactive_loop(
                cwd=self.cwd,
                repo=self.repo,
                db=self.db,
            )

        self.assertEqual(exit_code, 0)
        output = stdout_capture.getvalue()
        self.assertIn("zaman aşımı", output.lower())
        self.assertNotIn("Traceback (most recent call last)", output)
        self.assertEqual(len(fake_runtime.calls), 1)

    # -------------------------------------------------------------
    # B. Next prompt after TimeoutError
    # -------------------------------------------------------------
    def test_next_prompt_after_timeout_error(self) -> None:
        """Prompt 1 times out; prompt 2 succeeds; shell remains completely usable."""
        fake_runtime = FakeRuntime(side_effects=[
            TimeoutError("turn timeout"),
            CX2ExecutionResult(
                blocked=False,
                thread_id="fake-thread-2",
                session_mode="NEW",
                plan={"blocked": False},
                quota={},
                final_result=None,
                raw_turn_result=None,
                attempts_used=1,
                escalations=0,
            ),
        ])

        inputs = ["prompt that times out", "prompt that succeeds", "/exit"]
        stdout_capture = io.StringIO()

        with patch("builtins.input", side_effect=inputs), \
             patch("sys.stdout", stdout_capture), \
             patch("cx2_cli.CX2Runtime", return_value=fake_runtime):
            exit_code = cx2_cli.interactive_loop(
                cwd=self.cwd,
                repo=self.repo,
                db=self.db,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(fake_runtime.calls), 2)
        self.assertEqual(fake_runtime.calls[0]["prompt"], "prompt that times out")
        self.assertEqual(fake_runtime.calls[1]["prompt"], "prompt that succeeds")

    # -------------------------------------------------------------
    # C. AppServerProtocolError containment and transport reset
    # -------------------------------------------------------------
    def test_interactive_protocol_error_containment_and_recovery(self) -> None:
        """AppServerProtocolError must close broken runtime and allow fresh next prompt."""
        fake_runtime = FakeRuntime(side_effects=[
            AppServerProtocolError("App Server pipe broken"),
            CX2ExecutionResult(
                blocked=False,
                thread_id="fake-thread-3",
                session_mode="NEW",
                plan={"blocked": False},
                quota={},
                final_result=None,
                raw_turn_result=None,
                attempts_used=1,
                escalations=0,
            ),
        ])

        inputs = ["prompt with broken pipe", "prompt with recovered transport", "/exit"]
        stdout_capture = io.StringIO()

        with patch("builtins.input", side_effect=inputs), \
             patch("sys.stdout", stdout_capture), \
             patch("cx2_cli.CX2Runtime", return_value=fake_runtime):
            exit_code = cx2_cli.interactive_loop(
                cwd=self.cwd,
                repo=self.repo,
                db=self.db,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(fake_runtime.calls), 2)
        output = stdout_capture.getvalue()
        self.assertIn("bağlantı", output.lower())
        self.assertNotIn("Traceback (most recent call last)", output)

    # -------------------------------------------------------------
    # D. RuntimeError containment
    # -------------------------------------------------------------
    def test_interactive_runtime_error_containment(self) -> None:
        """RuntimeError / CX2RuntimeError must not kill the interactive loop."""
        fake_runtime = FakeRuntime(side_effects=[
            CX2RuntimeError("Execution plan has no attempts"),
            RuntimeError("Generic runtime failure"),
        ])

        inputs = ["prompt 1", "prompt 2", "/exit"]
        stdout_capture = io.StringIO()

        with patch("builtins.input", side_effect=inputs), \
             patch("sys.stdout", stdout_capture), \
             patch("cx2_cli.CX2Runtime", return_value=fake_runtime):
            exit_code = cx2_cli.interactive_loop(
                cwd=self.cwd,
                repo=self.repo,
                db=self.db,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(fake_runtime.calls), 2)
        output = stdout_capture.getvalue()
        self.assertNotIn("Traceback (most recent call last)", output)

    # -------------------------------------------------------------
    # E. ValueError containment
    # -------------------------------------------------------------
    def test_interactive_value_error_containment(self) -> None:
        """ValueError (e.g. parameter validation) must be displayed cleanly without killing shell."""
        fake_runtime = FakeRuntime(side_effects=[ValueError("Unsupported approval policy: 'always'")])

        inputs = ["invalid param prompt", "/exit"]
        stdout_capture = io.StringIO()

        with patch("builtins.input", side_effect=inputs), \
             patch("sys.stdout", stdout_capture), \
             patch("cx2_cli.CX2Runtime", return_value=fake_runtime):
            exit_code = cx2_cli.interactive_loop(
                cwd=self.cwd,
                repo=self.repo,
                db=self.db,
            )

        self.assertEqual(exit_code, 0)
        output = stdout_capture.getvalue()
        self.assertIn("geçersiz", output.lower())
        self.assertNotIn("Traceback (most recent call last)", output)

    # -------------------------------------------------------------
    # F. Unexpected Exception containment and crash log
    # -------------------------------------------------------------
    def test_interactive_unexpected_exception_logged_and_contained(self) -> None:
        """Unexpected Exception must be logged and contained, keeping the shell alive."""
        fake_runtime = FakeRuntime(side_effects=[OSError("Unexpected disk I/O error")])

        inputs = ["unexpected fail prompt", "/exit"]
        stdout_capture = io.StringIO()
        logged_crashes: list[str] = []

        def fake_write_log(exc):
            logged_crashes.append(str(exc))

        with patch("builtins.input", side_effect=inputs), \
             patch("sys.stdout", stdout_capture), \
             patch("cx2_cli._write_crash_log", side_effect=fake_write_log), \
             patch("cx2_cli.CX2Runtime", return_value=fake_runtime):
            exit_code = cx2_cli.interactive_loop(
                cwd=self.cwd,
                repo=self.repo,
                db=self.db,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(logged_crashes), 1)
        self.assertIn("disk I/O error", logged_crashes[0])

    # -------------------------------------------------------------
    # G. KeyboardInterrupt regression (preserves Ctrl+C behavior)
    # -------------------------------------------------------------
    def test_interactive_keyboard_interrupt_behavior_preserved(self) -> None:
        """KeyboardInterrupt during turn execution prints stop message and keeps loop alive."""
        fake_runtime = FakeRuntime(side_effects=[
            KeyboardInterrupt(),
            CX2ExecutionResult(
                blocked=False,
                thread_id="fake-thread-4",
                session_mode="NEW",
                plan={"blocked": False},
                quota={},
                final_result=None,
                raw_turn_result=None,
                attempts_used=1,
                escalations=0,
            ),
        ])

        inputs = ["interrupted prompt", "next prompt", "/exit"]
        stdout_capture = io.StringIO()

        with patch("builtins.input", side_effect=inputs), \
             patch("sys.stdout", stdout_capture), \
             patch("cx2_cli.CX2Runtime", return_value=fake_runtime):
            exit_code = cx2_cli.interactive_loop(
                cwd=self.cwd,
                repo=self.repo,
                db=self.db,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(fake_runtime.calls), 2)
        output = stdout_capture.getvalue()
        self.assertIn("durduruldu", output.lower())

    # -------------------------------------------------------------
    # H. SystemExit / BaseException is NOT swallowed
    # -------------------------------------------------------------
    def test_system_exit_not_swallowed(self) -> None:
        """SystemExit must NOT be caught by Exception handlers; it must escape."""
        fake_runtime = FakeRuntime(side_effects=[SystemExit(42)])

        inputs = ["exit prompt"]

        with patch("builtins.input", side_effect=inputs), \
             patch("cx2_cli.CX2Runtime", return_value=fake_runtime):
            with self.assertRaises(SystemExit) as ctx:
                cx2_cli.interactive_loop(
                    cwd=self.cwd,
                    repo=self.repo,
                    db=self.db,
                )
            self.assertEqual(ctx.exception.code, 42)

    # -------------------------------------------------------------
    # I. Failure in recovery (adversarial test)
    # -------------------------------------------------------------
    def test_failure_in_recovery_does_not_crash_shell(self) -> None:
        """If runtime.close() or crash logging raises during recovery, shell still survives."""
        fake_runtime = FakeRuntime(side_effects=[AppServerProtocolError("pipe broken")])
        fake_runtime.close = MagicMock(side_effect=RuntimeError("Cleanup error inside close()"))

        inputs = ["prompt", "/exit"]
        stdout_capture = io.StringIO()

        with patch("builtins.input", side_effect=inputs), \
             patch("sys.stdout", stdout_capture), \
             patch("cx2_cli._write_crash_log", side_effect=OSError("Read-only log path")), \
             patch("cx2_cli.CX2Runtime", return_value=fake_runtime):
            exit_code = cx2_cli.interactive_loop(
                cwd=self.cwd,
                repo=self.repo,
                db=self.db,
            )

        self.assertEqual(exit_code, 0)

    # -------------------------------------------------------------
    # J. One-shot mode error handling
    # -------------------------------------------------------------
    def test_one_shot_timeout_returns_nonzero(self) -> None:
        """One-shot mode with TimeoutError prints diagnostic and returns exit code 1."""
        fake_runtime = FakeRuntime(side_effects=[TimeoutError("turn timeout")])
        stderr_capture = io.StringIO()

        with patch("sys.stderr", stderr_capture), \
             patch("cx2_cli.CX2Runtime", return_value=fake_runtime):
            exit_code = cx2_cli.execute_one_shot(
                "one shot prompt",
                cwd=self.cwd,
                repo=self.repo,
                db=self.db,
            )

        self.assertEqual(exit_code, 1)
        err_output = stderr_capture.getvalue()
        self.assertIn("zaman aşımı", err_output.lower())
        self.assertNotIn("Traceback (most recent call last)", err_output)

    def test_one_shot_protocol_error_returns_nonzero(self) -> None:
        """One-shot mode with AppServerProtocolError returns 1 and logs crash."""
        fake_runtime = FakeRuntime(side_effects=[AppServerProtocolError("transport closed")])
        stderr_capture = io.StringIO()
        logged: list[str] = []

        with patch("sys.stderr", stderr_capture), \
             patch("cx2_cli._write_crash_log", side_effect=lambda exc: logged.append(str(exc))), \
             patch("cx2_cli.CX2Runtime", return_value=fake_runtime):
            exit_code = cx2_cli.execute_one_shot(
                "one shot prompt",
                cwd=self.cwd,
                repo=self.repo,
                db=self.db,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(logged), 1)
        err_output = stderr_capture.getvalue()
        self.assertIn("bağlantı", err_output.lower())


if __name__ == "__main__":
    unittest.main()
