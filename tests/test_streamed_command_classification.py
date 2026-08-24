from __future__ import annotations

"""
CX2 2.0.11 Phase 0/1 Regression Test Suite
Live App Server Command-Output Reconciliation & Bounded Stream Classification.
"""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

from verification_gate import (
    CommandExecutionSummary,
    classify_command,
    classify_command_outcome,
)
from bounded_verification_runner import (
    BoundedExecutionResult,
    is_verification_command_eligible,
)
from turn_runner import (
    MAX_COMMAND_OUTPUT_BYTES_RETAINED,
    StreamingTurnRunner,
    TurnApprovalState,
    TurnRunResult,
    extract_command_diagnostic_text,
    safe_item_summary,
)


class SyntheticTurnClient:
    """Minimal synthetic client for turn runner event dispatching."""

    def __init__(self) -> None:
        self.recorded_requests: list[tuple[str, dict]] = []

    def request(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        self.recorded_requests.append((method, params))
        return {"result": {}}

    def respond(self, request_id: Any, result: dict) -> None:
        pass


class TestStreamedCommandClassification(unittest.TestCase):

    def test_extract_command_diagnostic_text_precedence(self) -> None:
        """Inline aggregatedOutput / output / error / stderr take precedence over accumulated stream."""
        # 1. aggregatedOutput inline
        item_agg = {"aggregatedOutput": "INLINE_AGG_OUT"}
        self.assertEqual(
            extract_command_diagnostic_text(item_agg, accumulated_stream="STREAMED_TEXT"),
            "INLINE_AGG_OUT",
        )

        # 2. output inline
        item_out = {"output": "INLINE_OUT"}
        self.assertEqual(
            extract_command_diagnostic_text(item_out, accumulated_stream="STREAMED_TEXT"),
            "INLINE_OUT",
        )

        # 3. error inline
        item_err = {"error": "INLINE_ERR"}
        self.assertEqual(
            extract_command_diagnostic_text(item_err, accumulated_stream="STREAMED_TEXT"),
            "INLINE_ERR",
        )

        # 4. stderr inline
        item_stderr = {"stderr": "INLINE_STDERR"}
        self.assertEqual(
            extract_command_diagnostic_text(item_stderr, accumulated_stream="STREAMED_TEXT"),
            "INLINE_STDERR",
        )

        # 5. Streamed fallback when inline is absent or empty
        item_empty = {"id": "call-1", "type": "commandExecution"}
        self.assertEqual(
            extract_command_diagnostic_text(item_empty, accumulated_stream="STREAMED_FALLBACK"),
            "STREAMED_FALLBACK",
        )

        # 6. Both empty
        self.assertEqual(
            extract_command_diagnostic_text(item_empty, accumulated_stream=""),
            "",
        )

    def test_extract_command_diagnostic_text_bounded_bytes(self) -> None:
        """Diagnostic text is strictly capped to max_bytes without raising decoding errors."""
        huge_text = "A" * (MAX_COMMAND_OUTPUT_BYTES_RETAINED + 1000)
        res = extract_command_diagnostic_text({}, accumulated_stream=huge_text)
        self.assertEqual(len(res.encode("utf-8")), MAX_COMMAND_OUTPUT_BYTES_RETAINED)

    def test_safe_item_summary_populates_output_snippet_from_stream(self) -> None:
        """safe_item_summary extracts output_snippet from accumulated stream when inline is missing."""
        item = {
            "id": "cmd-1",
            "type": "commandExecution",
            "command": "npx jest",
            "status": "failed",
            "exitCode": 1,
        }
        summary = safe_item_summary(item, accumulated_stream="EPERM: operation not permitted")
        self.assertEqual(summary["output_snippet"], "EPERM: operation not permitted")

    def test_output_delta_bounded_accumulation_in_turn_runner(self) -> None:
        """StreamingTurnRunner strictly bounds result.command_output retention."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        result = TurnRunResult(thread_id="th-1", turn_id="turn-1")

        chunk = "X" * (100 * 1024)
        for _ in range(10):  # 10 * 100 KiB = 1000 KiB > 512 KiB cap
            runner._handle_notification(
                result,
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": {"itemId": "call-large", "delta": chunk},
                },
            )

        accumulated = result.command_output.get("call-large", "")
        self.assertLessEqual(
            len(accumulated.encode("utf-8")),
            MAX_COMMAND_OUTPUT_BYTES_RETAINED,
        )

    def test_output_delta_interleaved_isolation(self) -> None:
        """Output deltas for interleaved item IDs never cross-contaminate."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        result = TurnRunResult(thread_id="th-1", turn_id="turn-1")

        # Stream delta A1, B1, A2, B2
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": "item-A", "delta": "AAA_1"}},
        )
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": "item-B", "delta": "BBB_1"}},
        )
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": "item-A", "delta": "AAA_2"}},
        )
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": "item-B", "delta": "BBB_2"}},
        )

        # Complete A
        runner._handle_notification(
            result,
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "item-A",
                        "type": "commandExecution",
                        "command": "cmdA",
                        "exitCode": 0,
                    }
                },
            },
        )
        # Complete B
        runner._handle_notification(
            result,
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "item-B",
                        "type": "commandExecution",
                        "command": "cmdB",
                        "exitCode": 0,
                    }
                },
            },
        )

        self.assertEqual(result.command_executions[0]["classification_text"], "AAA_1AAA_2")
        self.assertEqual(result.command_executions[1]["classification_text"], "BBB_1BBB_2")

    def test_utf8_multibyte_streamed_chunks(self) -> None:
        """Multibyte UTF-8 characters delivered in separate stream deltas decode cleanly."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        result = TurnRunResult(thread_id="th-1", turn_id="turn-1")

        part1 = "Türkçe test: "
        part2 = "🇹🇷 ✨ ğüşıöç"
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": "utf8-item", "delta": part1}},
        )
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": "utf8-item", "delta": part2}},
        )
        runner._handle_notification(
            result,
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "utf8-item",
                        "type": "commandExecution",
                        "command": "npx jest",
                        "exitCode": 0,
                    }
                },
            },
        )

        self.assertEqual(
            result.command_executions[0]["classification_text"],
            "Türkçe test: 🇹🇷 ✨ ğüşıöç",
        )

    def test_production_defect_streamed_eperm_triggers_verification_offer(self) -> None:
        """
        Exact regression test for the Phase 6 production defect:
        Codex App Server streams EPERM error via outputDelta, and item/completed has no inline output.
        The streamed fallback must populate classification_text and trigger the bounded verification offer.
        """
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        runner.current_permissions = ":read-only"
        runner.current_cwd = r"C:\Projects\docker_projects\hibrit_app\backend"

        # Track prompt offers
        offers = []
        def intercept_prompt(*args, **kwargs):
            offers.append(kwargs)
            return "accept"
        runner._safe_approval_prompt = intercept_prompt

        # Mock bounded execution to avoid spawning real host process in unit test
        mock_res = BoundedExecutionResult(
            command="npx jest --runInBand",
            cwd=runner.current_cwd,
            exit_code=0,
            stdout="PASS backend tests",
            stderr="",
            duration_ms=1200,
            output_snippet="PASS backend tests",
            classification_text="PASS backend tests",
            bounded_host_execution=True,
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_bytes_total=20,
            stderr_bytes_total=0,
        )

        with patch("turn_runner.execute_bounded_verification_command", return_value=mock_res):
            result = TurnRunResult(thread_id="th-prod", turn_id="turn-prod")

            # 1. Started
            runner._handle_notification(
                result,
                {
                    "method": "item/started",
                    "params": {
                        "item": {
                            "id": "call-jest-1",
                            "type": "commandExecution",
                            "command": '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command \'npx jest --runInBand\'',
                            "cwd": runner.current_cwd,
                            "status": "inProgress",
                        }
                    },
                },
            )

            # 2. Output delta containing Jest sandbox EPERM error
            runner._handle_notification(
                result,
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": {
                        "itemId": "call-jest-1",
                        "delta": "EPERM: operation not permitted, mkdir 'C:\\Users\\muugo\\.codex-agent-cache\\tmp\\jest'\n",
                    },
                },
            )

            # 3. Completed item without inline output
            runner._handle_notification(
                result,
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "id": "call-jest-1",
                            "type": "commandExecution",
                            "command": '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command \'npx jest --runInBand\'',
                            "cwd": runner.current_cwd,
                            "status": "failed",
                            "exitCode": 1,
                            "durationMs": 1781,
                        }
                    },
                },
            )

            # Assertions
            self.assertEqual(len(result.command_executions), 1)
            rec = result.command_executions[0]
            self.assertEqual(len(offers), 1)
            self.assertEqual(offers[0]["title"], "Verification command requires writable runtime access")
            self.assertIn("Command: npx jest --runInBand", offers[0]["details"])
            self.assertTrue(rec["bounded_host_execution"])
            self.assertEqual(rec["exit_code"], 0)
            self.assertEqual(rec["classification_text"], "PASS backend tests")

    def test_unrelated_command_with_streamed_error_receives_no_offer(self) -> None:
        """Non-verification commands or generic errors do not trigger bounded verification offers."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        runner.current_permissions = ":read-only"

        offers = []
        runner._safe_approval_prompt = lambda *args, **kwargs: offers.append(kwargs) or "accept"

        result = TurnRunResult(thread_id="th-1", turn_id="turn-1")

        # Command: echo hello (Category: OTHER) failing with exit 1
        runner._handle_notification(
            result,
            {
                "method": "item/commandExecution/outputDelta",
                "params": {"itemId": "call-other", "delta": "EPERM: operation not permitted"},
            },
        )
        runner._handle_notification(
            result,
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "call-other",
                        "type": "commandExecution",
                        "command": "echo hello",
                        "status": "failed",
                        "exitCode": 1,
                    }
                },
            },
        )

        self.assertEqual(len(offers), 0)
        self.assertFalse(result.command_executions[0]["bounded_host_execution"])

    def test_genuine_test_failure_via_stream_receives_no_offer(self) -> None:
        """Genuine test failure (FAIL src/index.test.ts) classified as FAILED / TEST_FAILURE receives no offer."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        runner.current_permissions = ":read-only"

        offers = []
        runner._safe_approval_prompt = lambda *args, **kwargs: offers.append(kwargs) or "accept"

        result = TurnRunResult(thread_id="th-1", turn_id="turn-1")

        runner._handle_notification(
            result,
            {
                "method": "item/commandExecution/outputDelta",
                "params": {
                    "itemId": "call-test-fail",
                    "delta": "FAIL src/app.test.ts\n ● App › should calculate\n Expected: 2\n Received: 1\n",
                },
            },
        )
        runner._handle_notification(
            result,
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "call-test-fail",
                        "type": "commandExecution",
                        "command": "npx jest",
                        "status": "failed",
                        "exitCode": 1,
                    }
                },
            },
        )

        self.assertEqual(len(offers), 0)
        self.assertFalse(result.command_executions[0]["bounded_host_execution"])

    def test_fake_eperm_spoofing_classification(self) -> None:
        """
        When a test script deliberately prints 'EPERM: operation not permitted' on exit 1,
        it classifies as SANDBOX_DENIED and presents the explicit human approval prompt.
        Crucially: host execution occurs ONLY IF the human user accepts; if declined, 0 host executions occur.
        """
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        runner.current_permissions = ":read-only"

        # 1. User declines
        runner._safe_approval_prompt = lambda *args, **kwargs: "decline"
        result_decline = TurnRunResult(thread_id="th-fake", turn_id="turn-fake-1")

        runner._handle_notification(
            result_decline,
            {
                "method": "item/commandExecution/outputDelta",
                "params": {
                    "itemId": "call-fake-1",
                    "delta": "EPERM: operation not permitted\n",
                },
            },
        )
        runner._handle_notification(
            result_decline,
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "call-fake-1",
                        "type": "commandExecution",
                        "command": "npx jest",
                        "status": "failed",
                        "exitCode": 1,
                    }
                },
            },
        )

        self.assertFalse(result_decline.command_executions[0]["bounded_host_execution"])
        self.assertEqual(result_decline.command_executions[0]["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
