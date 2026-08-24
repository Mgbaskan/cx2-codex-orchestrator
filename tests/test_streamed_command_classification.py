from __future__ import annotations

"""
CX2 2.0.11 Phase 1.1 Adversarial Qualification Test Suite
Streamed Diagnostic Window Hardening, Head+Tail Bounded Retention, Event Order,
Adversarial Late-Diagnostic Tests, Inline aggregatedOutput, Interleaved Isolation,
and Memory Soak.
"""

import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any
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
    BoundedDiagnosticAccumulator,
    MAX_COMMAND_OUTPUT_BYTES_RETAINED,
    MAX_HEAD_BYTES,
    MAX_TAIL_BYTES,
    StreamingTurnRunner,
    TurnApprovalState,
    TurnRunResult,
    extract_bounded_window_text,
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

    # =========================================================
    # 1. RETENTION ALGORITHM & BOUNDED WINDOW TESTS
    # =========================================================

    def test_accumulator_small_payload_exact(self) -> None:
        """Accumulator preserves small payloads verbatim without truncation."""
        accum = BoundedDiagnosticAccumulator(max_total_bytes=512 * 1024, max_head_bytes=64 * 1024)
        accum.push("line 1\n")
        accum.push("line 2\n")
        accum.push("line 3\n")
        self.assertEqual(accum.get_diagnostic_text(), "line 1\nline 2\nline 3\n")
        self.assertEqual(accum.total_bytes_streamed, len("line 1\nline 2\nline 3\n".encode("utf-8")))

    def test_accumulator_head_tail_window_preservation(self) -> None:
        """Accumulator preserves first 64 KiB (head) and last 448 KiB (tail) when stream > 512 KiB."""
        accum = BoundedDiagnosticAccumulator(max_total_bytes=512 * 1024, max_head_bytes=64 * 1024)

        # 1. Push head sentinel
        accum.push("HEAD_START_MARKER\n")

        # 2. Push 700 KiB of middle padding in chunks
        chunk = "M" * 1024 + "\n"
        for _ in range(700):
            accum.push(chunk)

        # 3. Push tail sentinel
        accum.push("TAIL_END_MARKER_EPERM\n")

        text = accum.get_diagnostic_text()
        self.assertIn("HEAD_START_MARKER", text)
        self.assertIn("TAIL_END_MARKER_EPERM", text)
        self.assertIn("[truncated", text)
        self.assertLessEqual(len(text.encode("utf-8")), MAX_COMMAND_OUTPUT_BYTES_RETAINED + 500)

    def test_extract_bounded_window_text_static(self) -> None:
        """extract_bounded_window_text extracts head + tail on large static strings."""
        prefix = "START_PREFIX_" + "A" * (70 * 1024)
        suffix = "B" * (500 * 1024) + "_END_SUFFIX"
        full_text = prefix + suffix

        bounded = extract_bounded_window_text(full_text, max_total_bytes=512 * 1024, max_head_bytes=64 * 1024)
        self.assertIn("START_PREFIX_", bounded)
        self.assertIn("_END_SUFFIX", bounded)
        self.assertIn("[truncated", bounded)
        self.assertLessEqual(len(bounded.encode("utf-8")), 512 * 1024 + 500)

    # =========================================================
    # 2. ADVERSARIAL LATE-DIAGNOSTIC TESTS (CASES A-E)
    # =========================================================

    def test_case_a_late_sandbox_error_after_700kib(self) -> None:
        """CASE A: 700+ KiB ordinary output followed by late EPERM sandbox error."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        result = TurnRunResult(thread_id="th-1", turn_id="turn-1")
        item_id = "cmd-late-a"

        # Stream 700 KiB ordinary output
        chunk = "LOG: test processing batch data line...\n"
        chunk_bytes = len(chunk.encode("utf-8"))
        iterations = (700 * 1024) // chunk_bytes + 1
        for _ in range(iterations):
            runner._handle_notification(
                result,
                {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": chunk}},
            )

        # Stream late EPERM error
        late_err = "EPERM: operation not permitted, mkdir 'C:\\Users\\muugo\\.codex-agent-cache\\tmp\\jest'\n"
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": late_err}},
        )

        # Complete command without inline output
        runner._handle_notification(
            result,
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": item_id,
                        "type": "commandExecution",
                        "command": "npx jest --runInBand",
                        "status": "failed",
                        "exitCode": 1,
                    }
                },
            },
        )

        self.assertEqual(len(result.command_executions), 1)
        rec = result.command_executions[0]
        self.assertIn("EPERM: operation not permitted", rec["classification_text"])

        summary = CommandExecutionSummary(
            command=rec["command"],
            exit_code=rec["exit_code"],
            duration_ms=rec["duration_ms"],
            sequence=rec["sequence"],
            categories=rec["categories"],
            is_masked=rec["is_masked"],
            display_command=rec["display_command"],
            output_snippet=rec["output_snippet"],
            classification_text=rec["classification_text"],
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "BLOCKED")
        self.assertEqual(outcome.reason_code, "SANDBOX_DENIED")
        self.assertTrue(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_case_b_5mb_late_sandbox_error(self) -> None:
        """CASE B: 5 MB ordinary stream followed by EPERM error near the end."""
        accum = BoundedDiagnosticAccumulator(max_total_bytes=512 * 1024, max_head_bytes=64 * 1024)

        # 5 MB of 64 KiB chunks
        chunk = "D" * (64 * 1024)
        for _ in range(80):  # 80 * 64 KiB = 5120 KiB = 5 MB
            accum.push(chunk)

        accum.push("\nEPERM: operation not permitted, mkdir 'C:\\Users\\muugo\\.codex-agent-cache\\tmp\\jest'\n")

        diag = accum.get_diagnostic_text()
        self.assertIn("EPERM: operation not permitted", diag)
        self.assertLessEqual(len(diag.encode("utf-8")), MAX_COMMAND_OUTPUT_BYTES_RETAINED + 500)

    def test_case_c_late_genuine_test_failure(self) -> None:
        """CASE C: >512 KiB ordinary test output followed by a genuine Jest failure."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        result = TurnRunResult(thread_id="th-1", turn_id="turn-1")
        item_id = "cmd-late-c"

        # Stream >512 KiB passing tests
        for i in range(1000):
            runner._handle_notification(
                result,
                {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": f"PASS src/test_{i}.ts (1.2s)\n"}},
            )

        # Stream late test failure
        fail_snippet = (
            "FAIL src/users.service.spec.ts\n"
            "  ● UsersService › should validate password\n"
            "    Expected: true\n"
            "    Received: false\n"
            "Test Suites: 1 failed, 999 passed, 1000 total\n"
            "Tests: 1 failed, 999 passed, 1000 total\n"
        )
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": fail_snippet}},
        )

        runner._handle_notification(
            result,
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": item_id,
                        "type": "commandExecution",
                        "command": "npx jest --runInBand",
                        "status": "failed",
                        "exitCode": 1,
                    }
                },
            },
        )

        rec = result.command_executions[0]
        self.assertIn("FAIL src/users.service.spec.ts", rec["classification_text"])

        summary = CommandExecutionSummary(
            command=rec["command"],
            exit_code=rec["exit_code"],
            duration_ms=rec["duration_ms"],
            sequence=rec["sequence"],
            categories=rec["categories"],
            is_masked=rec["is_masked"],
            display_command=rec["display_command"],
            output_snippet=rec["output_snippet"],
            classification_text=rec["classification_text"],
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "TEST_FAILURE")
        self.assertFalse(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_case_d_late_executable_not_found(self) -> None:
        """CASE D: >512 KiB output prefix followed by executable-not-found diagnostic."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        result = TurnRunResult(thread_id="th-1", turn_id="turn-1")
        item_id = "cmd-late-d"

        for _ in range(600):
            runner._handle_notification(
                result,
                {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": "searching directory...\n"}},
            )

        err = "jest : The term 'jest' is not recognized as the name of a cmdlet, function, script file, or operable program.\n"
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": err}},
        )

        runner._handle_notification(
            result,
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": item_id,
                        "type": "commandExecution",
                        "command": "jest",
                        "status": "failed",
                        "exitCode": 1,
                    }
                },
            },
        )

        rec = result.command_executions[0]
        self.assertIn("is not recognized", rec["classification_text"])

        summary = CommandExecutionSummary(
            command=rec["command"],
            exit_code=rec["exit_code"],
            duration_ms=rec["duration_ms"],
            sequence=rec["sequence"],
            categories=rec["categories"],
            is_masked=rec["is_masked"],
            display_command=rec["display_command"],
            output_snippet=rec["output_snippet"],
            classification_text=rec["classification_text"],
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "BLOCKED")
        self.assertEqual(outcome.reason_code, "EXECUTABLE_NOT_FOUND")

    def test_case_e_unicode_multibyte_boundary_and_late_eperm(self) -> None:
        """CASE E: Multibyte Turkish UTF-8 characters across chunk boundary + late EPERM."""
        accum = BoundedDiagnosticAccumulator(max_total_bytes=512 * 1024, max_head_bytes=64 * 1024)

        # Stream 600 KiB of Turkish unicode patterns
        turkish_pattern = "Türkçe doğrulama testi: 🇹🇷 ✨ ğüşıöç — işlem no: "
        for i in range(15000):
            accum.push(f"{turkish_pattern}{i}\n")

        accum.push("SON_HATA: EPERM: operation not permitted, mkdir 'C:\\Users\\muugo\\.codex-agent-cache\\tmp\\jest'\n")

        text = accum.get_diagnostic_text()
        self.assertIn("Türkçe doğrulama", text)
        self.assertIn("SON_HATA: EPERM: operation not permitted", text)
        self.assertLessEqual(len(text.encode("utf-8")), MAX_COMMAND_OUTPUT_BYTES_RETAINED + 500)

    # =========================================================
    # 3. INLINE aggregatedOutput QUALIFICATION
    # =========================================================

    def test_aggregated_output_small_and_large(self) -> None:
        """extract_command_diagnostic_text handles small, 512KB, and 5MB aggregatedOutput properly."""
        # Small
        small_item = {"aggregatedOutput": "PASS all tests"}
        self.assertEqual(extract_command_diagnostic_text(small_item), "PASS all tests")

        # 5MB with error at beginning (head preserved)
        big_start = {"aggregatedOutput": "EPERM: start error\n" + "X" * (5 * 1024 * 1024)}
        res_start = extract_command_diagnostic_text(big_start)
        self.assertIn("EPERM: start error", res_start)
        self.assertIn("[truncated", res_start)
        self.assertLessEqual(len(res_start.encode("utf-8")), MAX_COMMAND_OUTPUT_BYTES_RETAINED + 500)

        # 5MB with error at end (tail preserved)
        big_end = {"aggregatedOutput": "X" * (5 * 1024 * 1024) + "\nEPERM: operation not permitted, mkdir tmp\\jest\n"}
        res_end = extract_command_diagnostic_text(big_end)
        self.assertIn("EPERM: operation not permitted", res_end)
        self.assertIn("[truncated", res_end)
        self.assertLessEqual(len(res_end.encode("utf-8")), MAX_COMMAND_OUTPUT_BYTES_RETAINED + 500)

    # =========================================================
    # 4. MIXED INLINE + STREAM PRECEDENCE
    # =========================================================

    def test_mixed_inline_and_stream_precedence(self) -> None:
        """Documented precedence: non-empty inline fields take precedence over accumulated stream."""
        # 1. aggregatedOutput present + stream present -> inline wins
        item1 = {"aggregatedOutput": "INLINE_AGG"}
        self.assertEqual(extract_command_diagnostic_text(item1, accumulated_stream="STREAM_TEXT"), "INLINE_AGG")

        # 2. output present + stream present -> inline wins
        item2 = {"output": "INLINE_OUT"}
        self.assertEqual(extract_command_diagnostic_text(item2, accumulated_stream="STREAM_TEXT"), "INLINE_OUT")

        # 3. error present + stream present -> inline wins
        item3 = {"error": "INLINE_ERR"}
        self.assertEqual(extract_command_diagnostic_text(item3, accumulated_stream="STREAM_TEXT"), "INLINE_ERR")

        # 4. stderr present + stream present -> inline wins
        item4 = {"stderr": "INLINE_STDERR"}
        self.assertEqual(extract_command_diagnostic_text(item4, accumulated_stream="STREAM_TEXT"), "INLINE_STDERR")

        # 5. inline empty + stream present -> stream used
        item5 = {"id": "c1", "type": "commandExecution"}
        self.assertEqual(extract_command_diagnostic_text(item5, accumulated_stream="STREAM_USED"), "STREAM_USED")

        # 6. all empty -> empty
        self.assertEqual(extract_command_diagnostic_text({}, accumulated_stream=""), "")

    # =========================================================
    # 5. FAILURE-CONFLICT SEMANTICS (EPERM + GENUINE FAIL)
    # =========================================================

    def test_failure_conflict_semantics_no_auto_execution(self) -> None:
        """
        Commands containing BOTH EPERM-like text AND genuine test failure signatures.
        In all cases, NO automatic host execution ever occurs without explicit human approval.
        """
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        runner.current_permissions = ":read-only"

        # User declines
        runner._safe_approval_prompt = lambda *args, **kwargs: "decline"
        result = TurnRunResult(thread_id="th-conflict", turn_id="turn-conflict")

        mixed_output = (
            "EPERM: operation not permitted, mkdir tmp\\cache\n"
            "FAIL src/index.test.ts\n"
            "Tests: 1 failed, 1 total\n"
        )
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": "call-conf", "delta": mixed_output}},
        )
        runner._handle_notification(
            result,
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "call-conf",
                        "type": "commandExecution",
                        "command": "npx jest",
                        "status": "failed",
                        "exitCode": 1,
                    }
                },
            },
        )

        rec = result.command_executions[0]
        # Crucial security assertion: Never executed automatically
        self.assertFalse(rec["bounded_host_execution"])
        self.assertEqual(rec["exit_code"], 1)

    # =========================================================
    # 6. INTERLEAVED COMMAND STRESS & ISOLATION (20 COMMANDS)
    # =========================================================

    def test_interleaved_command_stress_20_commands(self) -> None:
        """Simulate 20 command items with interleaved outputDelta notifications."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        result = TurnRunResult(thread_id="th-stress", turn_id="turn-stress")

        num_commands = 20
        # Stream 5 chunks for each command interleaved
        for chunk_idx in range(5):
            for cmd_idx in range(num_commands):
                item_id = f"cmd-stress-{cmd_idx}"
                delta = f"[CMD_{cmd_idx}_CHUNK_{chunk_idx}]\n"
                runner._handle_notification(
                    result,
                    {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": delta}},
                )

        # Complete all commands
        for cmd_idx in range(num_commands):
            item_id = f"cmd-stress-{cmd_idx}"
            runner._handle_notification(
                result,
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "id": item_id,
                            "type": "commandExecution",
                            "command": f"test_cmd_{cmd_idx}",
                            "status": "completed",
                            "exitCode": 0,
                        }
                    },
                },
            )

        self.assertEqual(len(result.command_executions), num_commands)
        for cmd_idx in range(num_commands):
            rec = result.command_executions[cmd_idx]
            self.assertEqual(rec["command"], f"test_cmd_{cmd_idx}")
            # Assert only this command's tokens exist in its classification_text
            for other_idx in range(num_commands):
                if other_idx != cmd_idx:
                    self.assertNotIn(f"CMD_{other_idx}_", rec["classification_text"])
            for chunk_idx in range(5):
                self.assertIn(f"[CMD_{cmd_idx}_CHUNK_{chunk_idx}]", rec["classification_text"])

    # =========================================================
    # 7. MEMORY SOAK (100 MB SINGLE & MULTI-COMMAND)
    # =========================================================

    def test_memory_soak_100mb_single_command(self) -> None:
        """Push 100 MB through a single command accumulator and verify memory bound."""
        accum = BoundedDiagnosticAccumulator(max_total_bytes=512 * 1024, max_head_bytes=64 * 1024)

        # 100 MB in 1 MB chunks
        chunk = "M" * (1024 * 1024)
        for _ in range(100):
            accum.push(chunk)

        accum.push("\nFINAL_ERROR: EPERM: operation not permitted\n")

        self.assertEqual(accum.total_bytes_streamed, 100 * 1024 * 1024 + len("\nFINAL_ERROR: EPERM: operation not permitted\n"))
        diag = accum.get_diagnostic_text()
        self.assertIn("FINAL_ERROR: EPERM", diag)
        self.assertIn("[truncated", diag)
        self.assertLessEqual(len(diag.encode("utf-8")), MAX_COMMAND_OUTPUT_BYTES_RETAINED + 500)

    def test_memory_soak_10_commands_10mb_each(self) -> None:
        """Push 10 MB through 10 distinct command accumulators without memory explosion."""
        accumulators = [
            BoundedDiagnosticAccumulator(max_total_bytes=512 * 1024, max_head_bytes=64 * 1024)
            for _ in range(10)
        ]
        chunk = "K" * (512 * 1024)  # 512 KiB chunk
        for _ in range(20):          # 20 * 512 KiB = 10 MB per command
            for acc in accumulators:
                acc.push(chunk)

        for idx, acc in enumerate(accumulators):
            acc.push(f"\nEND_CMD_{idx}_EPERM\n")
            diag = acc.get_diagnostic_text()
            self.assertIn(f"END_CMD_{idx}_EPERM", diag)
            self.assertLessEqual(len(diag.encode("utf-8")), MAX_COMMAND_OUTPUT_BYTES_RETAINED + 500)


    # =========================================================
    # 8. EVENT ORDERING & LATE NOTIFICATION RECONCILIATION
    # =========================================================

    def test_late_output_delta_after_item_completed(self) -> None:
        """If outputDelta arrives after item/completed, diagnostic text is reconciled."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        result = TurnRunResult(thread_id="th-late", turn_id="turn-late")
        item_id = "cmd-late-order"

        # 1. item/started
        runner._handle_notification(
            result,
            {
                "method": "item/started",
                "params": {"item": {"id": item_id, "type": "commandExecution", "command": "npm test"}},
            },
        )

        # 2. item/completed arrives first (empty inline)
        runner._handle_notification(
            result,
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": item_id,
                        "type": "commandExecution",
                        "command": "npm test",
                        "status": "failed",
                        "exitCode": 1,
                    }
                },
            },
        )

        # 3. Late outputDelta arrives after item/completed
        runner._handle_notification(
            result,
            {
                "method": "item/commandExecution/outputDelta",
                "params": {"itemId": item_id, "delta": "EPERM: operation not permitted, mkdir tmp\\jest\n"},
            },
        )

        self.assertEqual(len(result.command_executions), 1)
        rec = result.command_executions[0]
        self.assertIn("EPERM: operation not permitted", rec["classification_text"])
        self.assertIn("EPERM: operation not permitted", rec["output_snippet"])


if __name__ == "__main__":
    unittest.main()
