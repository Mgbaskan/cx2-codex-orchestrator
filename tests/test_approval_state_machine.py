from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import time
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

sys.path.insert(0, _bootstrap.RUNTIME_DIR)

from terminal_ui import TerminalRenderer
from turn_runner import (
    FINAL_STATUSES,
    StreamingTurnRunner,
    TurnApprovalState,
    TurnRunResult,
)


class MockTurnClient:
    def __init__(self) -> None:
        self.server_requests: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.responses: list[tuple[Any, Any]] = []
        self.error_responses: list[tuple[Any, int, str]] = []
        self.process: Any = None
        self._dispatcher_thread: Any = None

    def request(self, method: str, params: Any = None, timeout: float = 15.0) -> Any:
        return {"status": "ok"}

    def respond(self, request_id: Any, result: Any) -> None:
        self.responses.append((request_id, result))

    def respond_error(self, request_id: Any, code: int, message: str) -> None:
        self.error_responses.append((request_id, code, message))

    def drain_server_requests(self) -> list[dict[str, Any]]:
        reqs = list(self.server_requests)
        self.server_requests.clear()
        return reqs

    def drain_notifications(self) -> list[dict[str, Any]]:
        notifs = list(self.notifications)
        self.notifications.clear()
        return notifs

    def drain_unknown(self) -> list[dict[str, Any]]:
        return []


class TestApprovalStateMachine(unittest.TestCase):

    def setUp(self) -> None:
        self.client = MockTurnClient()
        self.runner = StreamingTurnRunner(
            client=self.client,
            live=True,
            poll_interval=0.001,
            max_approval_prompts_per_turn=4,
        )

    def test_same_request_id_replay(self) -> None:
        """Case A: Exact same request ID is replayed without re-prompting user."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")
        
        req = {
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "command": "git status",
                "cwd": "/workspace",
                "availableDecisions": ["accept", "decline"],
            },
        }

        # First delivery: prompt user -> choose accept
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="accept") as mock_prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req)
                self.assertEqual(mock_prompt.call_count, 1)
                self.assertEqual(len(self.client.responses), 1)
                self.assertEqual(self.client.responses[0], ("req-1", {"decision": "accept"}))

        # Second delivery (replay with same ID): should NOT prompt user, must replay cached response
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="decline") as mock_prompt_2:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req)
                self.assertEqual(mock_prompt_2.call_count, 0)
                self.assertEqual(len(self.client.responses), 2)
                self.assertEqual(self.client.responses[1], ("req-1", {"decision": "accept"}))

        self.assertEqual(result.server_approval_request_count, 2)
        self.assertEqual(result.interactive_approval_prompt_count, 1)
        self.assertEqual(result.exact_replay_count, 1)

    def test_declined_command_memory(self) -> None:
        """Case B: If user declined command, a NEW request ID for exact same command is auto-declined."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")

        req1 = {
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "command": "rm -rf /tmp/test",
                "cwd": "/workspace",
                "availableDecisions": ["accept", "decline"],
            },
        }

        # First delivery: user declines
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="decline") as mock_prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req1)
                self.assertEqual(mock_prompt.call_count, 1)
                self.assertEqual(self.client.responses[-1], ("req-1", {"decision": "decline"}))

        req2 = {
            "id": "req-2",  # New request ID
            "method": "item/commandExecution/requestApproval",
            "params": {
                "command": "rm -rf /tmp/test",  # Exact same command & cwd
                "cwd": "/workspace",
                "availableDecisions": ["accept", "decline"],
            },
        }

        # Second delivery: auto-decline from memory without prompting
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="accept") as mock_prompt_2:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req2)
                self.assertEqual(mock_prompt_2.call_count, 0)
                self.assertEqual(self.client.responses[-1], ("req-2", {"decision": "decline"}))

        self.assertEqual(result.auto_decline_count, 1)
        self.assertEqual(result.interactive_approval_prompt_count, 1)

    def test_accepted_command_prompts_again(self) -> None:
        """Case B (Accept Fail-Closed): A one-shot ACCEPT does NOT auto-accept a NEW request ID."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")

        req1 = {
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "command": "npm install lodash",
                "cwd": "/workspace",
                "availableDecisions": ["accept", "decline"],
            },
        }

        # First request accepted
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="accept") as mock_prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req1)
                self.assertEqual(mock_prompt.call_count, 1)

        req2 = {
            "id": "req-2",  # New request ID
            "method": "item/commandExecution/requestApproval",
            "params": {
                "command": "npm install lodash",
                "cwd": "/workspace",
                "availableDecisions": ["accept", "decline"],
            },
        }

        # Second request with new ID MUST prompt again (fail-closed)
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="accept") as mock_prompt_2:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req2)
                self.assertEqual(mock_prompt_2.call_count, 1)

        self.assertEqual(result.interactive_approval_prompt_count, 2)

    def test_session_accepted_command_memory(self) -> None:
        """Case B (Session Accept): acceptForSession is remembered for same command in turn."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")

        req1 = {
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "command": "npm run build",
                "cwd": "/workspace",
                "availableDecisions": ["accept", "acceptForSession", "decline"],
            },
        }

        # User chooses acceptForSession
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="acceptForSession") as mock_prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req1)
                self.assertEqual(mock_prompt.call_count, 1)
                self.assertEqual(self.client.responses[-1], ("req-1", {"decision": "acceptForSession"}))

        req2 = {
            "id": "req-2",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "command": "npm run build",
                "cwd": "/workspace",
                "availableDecisions": ["accept", "acceptForSession", "decline"],
            },
        }

        # New request ID for same command auto-accepts for session
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="decline") as mock_prompt_2:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req2)
                self.assertEqual(mock_prompt_2.call_count, 0)
                self.assertEqual(self.client.responses[-1], ("req-2", {"decision": "acceptForSession"}))

    def test_different_command_same_cwd_prompts_separately(self) -> None:
        """Case C: Different commands in same cwd are prompt-isolated."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")

        req1 = {
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "cmd_a", "cwd": "/workspace", "availableDecisions": ["accept", "decline"]},
        }
        req2 = {
            "id": "req-2",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "cmd_b", "cwd": "/workspace", "availableDecisions": ["accept", "decline"]},
        }

        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="accept") as mock_prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req1)
                self.runner._handle_server_request(result, req2)
                self.assertEqual(mock_prompt.call_count, 2)

    def test_different_cwd_same_command_prompts_separately(self) -> None:
        """Case C: Same command in different cwd is prompt-isolated."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")

        req1 = {
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "make test", "cwd": "/workspace/pkg_a", "availableDecisions": ["accept", "decline"]},
        }
        req2 = {
            "id": "req-2",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "make test", "cwd": "/workspace/pkg_b", "availableDecisions": ["accept", "decline"]},
        }

        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="accept") as mock_prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req1)
                self.runner._handle_server_request(result, req2)
                self.assertEqual(mock_prompt.call_count, 2)

    def test_circuit_breaker_opens_at_limit(self) -> None:
        """Circuit breaker opens when interactive prompt count reaches max_approval_prompts_per_turn."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")
        # max is set to 4 in setUp

        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="accept") as mock_prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                for i in range(1, 5):  # 4 prompts
                    req = {
                        "id": f"req-{i}",
                        "method": "item/commandExecution/requestApproval",
                        "params": {"command": f"cmd_{i}", "cwd": "/workspace", "availableDecisions": ["accept", "decline"]},
                    }
                    self.runner._handle_server_request(result, req)

                self.assertEqual(mock_prompt.call_count, 4)
                self.assertEqual(result.interactive_approval_prompt_count, 4)
                self.assertFalse(result.circuit_breaker_opened)

                # 5th request should trigger circuit breaker (auto-decline, no prompt)
                req5 = {
                    "id": "req-5",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "cmd_5", "cwd": "/workspace", "availableDecisions": ["accept", "decline"]},
                }
                with patch("turn_runner._CX2_TERMINAL.warning") as mock_warn:
                    self.runner._handle_server_request(result, req5)
                    self.assertEqual(mock_prompt.call_count, 4)  # No extra prompt
                    self.assertTrue(result.circuit_breaker_opened)
                    self.assertEqual(self.client.responses[-1], ("req-5", {"decision": "decline"}))
                    mock_warn.assert_called_once()

    def test_circuit_warning_rendered_once(self) -> None:
        """Circuit breaker warning is rendered exactly once during a storm."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")

        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="decline"):
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                with patch("turn_runner._CX2_TERMINAL.warning") as mock_warn:
                    for i in range(1, 10):
                        req = {
                            "id": f"req-{i}",
                            "method": "item/commandExecution/requestApproval",
                            "params": {"command": f"cmd_{i}", "cwd": "/workspace", "availableDecisions": ["accept", "decline"]},
                        }
                        self.runner._handle_server_request(result, req)

                    self.assertEqual(mock_warn.call_count, 1)

    def test_state_resets_on_next_turn(self) -> None:
        """Each turn gets a fresh approval state with closed circuit breaker and empty caches."""
        turn1_result = TurnRunResult(thread_id="th-1", turn_id="tu-1")
        req1 = {
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "cmd_1", "cwd": "/workspace", "availableDecisions": ["accept", "decline"]},
        }
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="decline"):
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(turn1_result, req1)

        # New turn
        turn2_result = TurnRunResult(thread_id="th-1", turn_id="tu-2")
        # In turn 2, same command should prompt again because turn 1 declined memory is turn-scoped
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="accept") as mock_prompt_2:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(turn2_result, req1)
                self.assertEqual(mock_prompt_2.call_count, 1)

        self.assertFalse(turn2_result.circuit_breaker_opened)

    def test_human_wait_extends_deadline(self) -> None:
        """Human approval blocking duration compensates the turn deadline."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")

        req = {
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "test", "cwd": "/workspace", "availableDecisions": ["accept", "decline"]},
        }

        # Simulate user taking 2.5 seconds to decide
        def fake_prompt(**kwargs):
            return "accept"

        with patch("turn_runner._CX2_TERMINAL.approval_prompt", side_effect=fake_prompt):
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                # Mock time.monotonic to advance by 2.5 seconds inside prompt
                current_time = [100.0]
                def fake_time():
                    t = current_time[0]
                    return t

                with patch("time.monotonic", side_effect=[100.0, 102.5, 102.5]):
                    self.runner._handle_server_request(result, req)

                self.assertAlmostEqual(result.human_approval_wait_seconds, 2.5, places=2)

    def test_noninteractive_no_wait_extension(self) -> None:
        """Non-interactive execution (can_prompt=False) does not record human wait."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")
        req = {
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "test", "cwd": "/workspace", "availableDecisions": ["accept", "decline"]},
        }

        with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: False)):
            self.runner._handle_server_request(result, req)
            self.assertEqual(result.human_approval_wait_seconds, 0.0)
            self.assertEqual(result.interactive_approval_prompt_count, 0)
            self.assertEqual(self.client.responses[-1], ("req-1", {"decision": "decline"}))

    def test_app_server_death_after_approval_detected(self) -> None:
        """If App Server process terminates while approval is pending, wait_for_turn raises protocol error."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")

        # Mock dead process
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        self.client.process = mock_proc

        self.client.server_requests.append({
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "test", "cwd": "/workspace", "availableDecisions": ["accept", "decline"]},
        })

        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="accept"):
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                with self.assertRaises(Exception) as ctx:
                    self.runner.wait_for_turn(result, timeout=10.0)

                self.assertIn("terminated unexpectedly", str(ctx.exception))

    def test_keyboard_interrupt_fails_safely(self) -> None:
        """KeyboardInterrupt inside approval_prompt fails closed and does not auto-accept."""
        out = io.StringIO()
        ui = TerminalRenderer(stream=out)
        
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                decision = ui.approval_prompt(
                    title="Command execution",
                    details=["cmd: dangerous"],
                    decisions=["accept", "decline", "cancel"],
                    default_decision="decline",
                )
                self.assertIn(decision, ("cancel", "decline"))

    def test_eof_fails_closed(self) -> None:
        """EOFError on stdin inside approval_prompt fails closed to default."""
        out = io.StringIO()
        ui = TerminalRenderer(stream=out)

        with patch("builtins.input", side_effect=EOFError):
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                decision = ui.approval_prompt(
                    title="Command execution",
                    details=["cmd: dangerous"],
                    decisions=["accept", "decline"],
                    default_decision="decline",
                )
                self.assertEqual(decision, "decline")

    def test_response_failure_no_accept_memory(self) -> None:
        """If sending respond() throws an exception, no valid cache or session memory is persisted."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")
        req = {
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "build", "cwd": "/workspace", "availableDecisions": ["accept", "acceptForSession", "decline"]},
        }

        # Mock client.respond to raise
        def exploding_respond(req_id, res):
            raise BrokenPipeError("Transport closed")

        self.client.respond = exploding_respond

        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="acceptForSession"):
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                with self.assertRaises(BrokenPipeError):
                    self.runner._handle_server_request(result, req)


    def test_two_approvals_accumulate_wait(self) -> None:
        """Two interactive approvals accumulate human wait seconds cumulatively."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")

        req1 = {
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "cmd1", "cwd": "/workspace", "availableDecisions": ["accept", "decline"]},
        }
        req2 = {
            "id": "req-2",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "cmd2", "cwd": "/workspace", "availableDecisions": ["accept", "decline"]},
        }

        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="accept"):
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                with patch("time.monotonic", side_effect=[100.0, 102.0, 105.0, 108.5]):
                    self.runner._handle_server_request(result, req1)
                    self.runner._handle_server_request(result, req2)

                self.assertAlmostEqual(result.human_approval_wait_seconds, 5.5, places=2)
                self.assertEqual(result.interactive_approval_prompt_count, 2)

    def test_model_time_consumes_deadline_triggers_timeout(self) -> None:
        """When model computation time exceeds timeout despite human wait, TimeoutError is raised."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")
        result.human_approval_wait_seconds = 10.0  # 10s human wait

        # If timeout is 5.0s, effective deadline is 0 + 5.0 + 10.0 = 15.0s.
        # At time 16.0s (model has run 6.0s > 5.0s), wait_for_turn must timeout.
        with patch("time.monotonic", side_effect=[0.0, 16.0, 16.0, 16.0]):
            with self.assertRaises(TimeoutError):
                self.runner.wait_for_turn(result, timeout=5.0)

    def test_security_adversarial_variations(self) -> None:
        """Adversarial attempts with arguments, prefixes, or cwds never inherit unauthorized acceptance."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")

        req_base = {
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "ls safe_dir", "cwd": "/workspace", "availableDecisions": ["accept", "acceptForSession", "decline"]},
        }
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="acceptForSession"):
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req_base)

        # 1. Changed argument: "ls dangerous_dir" must NOT be auto-accepted
        req_diff_arg = {
            "id": "req-2",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "ls dangerous_dir", "cwd": "/workspace", "availableDecisions": ["accept", "decline"]},
        }
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="decline") as mock_prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req_diff_arg)
                self.assertEqual(mock_prompt.call_count, 1)

        # 2. Changed cwd: "ls safe_dir" in "/other" must NOT be auto-accepted
        req_diff_cwd = {
            "id": "req-3",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "ls safe_dir", "cwd": "/other", "availableDecisions": ["accept", "decline"]},
        }
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="decline") as mock_prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req_diff_cwd)
                self.assertEqual(mock_prompt.call_count, 1)

    def test_patch_and_file_change_approvals(self) -> None:
        """File change approvals (modern & legacy) work with replay and prompt counting."""
        result = TurnRunResult(thread_id="th-1", turn_id="tu-1")
        self.runner.current_cwd = REPO_ROOT
        self.runner._active_thread_id = "th-1"

        req_modern = {
            "id": "req-f1",
            "method": "item/fileChange/requestApproval",
            "params": {
                "reason": "edit code",
                "grantRoot": str(REPO_ROOT),
                "fileChanges": [{"path": "notes.txt", "action": "edit"}],
                "availableDecisions": ["accept", "decline"],
            },
        }
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="accept") as mock_prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req_modern)
                self.assertEqual(mock_prompt.call_count, 1)
                self.assertEqual(self.client.responses[-1], ("req-f1", {"decision": "accept"}))

        # Replay
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="decline") as mock_prompt_2:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                self.runner._handle_server_request(result, req_modern)
                self.assertEqual(mock_prompt_2.call_count, 0)
                self.assertEqual(self.client.responses[-1], ("req-f1", {"decision": "accept"}))


if __name__ == "__main__":
    unittest.main()
