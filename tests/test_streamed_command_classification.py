from __future__ import annotations

"""
CX2 2.0.11 Phase 1.1 / 1.2 Adversarial Qualification Test Suite
Streamed Diagnostic Window Hardening, Head+Tail Bounded Retention,
Failure Precedence (Strong Project Failure > Sandbox Permission Noise),
Conflict Matrix (A-G), Weak 'FAIL' Negative Controls,
True Late-Event Offer Reconciliation, Duplicate Protection,
Inline aggregatedOutput, Interleaved Isolation, and Memory Soak.
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

        for i in range(1000):
            runner._handle_notification(
                result,
                {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": f"PASS src/test_{i}.ts (1.2s)\n"}},
            )

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

        turkish_pattern = "Türkçe doğrulama testi: 🇹🇷 ✨ ğüşıöç — işlem no: "
        for i in range(15000):
            accum.push(f"{turkish_pattern}{i}\n")

        accum.push("SON_HATA: EPERM: operation not permitted, mkdir 'C:\\Users\\muugo\\.codex-agent-cache\\tmp\\jest'\n")

        text = accum.get_diagnostic_text()
        self.assertIn("Türkçe doğrulama", text)
        self.assertIn("SON_HATA: EPERM: operation not permitted", text)
        self.assertLessEqual(len(text.encode("utf-8")), MAX_COMMAND_OUTPUT_BYTES_RETAINED + 500)

    # =========================================================
    # 3. FAILURE PRECEDENCE & CONFLICT MATRIX (A-G)
    # =========================================================

    def test_matrix_a_eperm_only(self) -> None:
        """Matrix A: EPERM only -> BLOCKED / SANDBOX_DENIED / eligible = True."""
        summary = CommandExecutionSummary(
            command="npm test",
            exit_code=1,
            categories=["TEST"],
            classification_text="npm error code EPERM\nnpm error operation not permitted, mkdir tmp\\jest\n",
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "BLOCKED")
        self.assertEqual(outcome.reason_code, "SANDBOX_DENIED")
        self.assertTrue(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_matrix_b_jest_genuine_failure_only(self) -> None:
        """Matrix B: Jest genuine failure only -> FAILED / TEST_FAILURE / eligible = False."""
        summary = CommandExecutionSummary(
            command="npm test",
            exit_code=1,
            categories=["TEST"],
            classification_text="FAIL src/app.spec.ts\n  ● should pass\n    Expected: 1\n    Received: 2\nTest Suites: 1 failed, 1 total\n",
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "TEST_FAILURE")
        self.assertFalse(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_matrix_c_eperm_plus_jest_genuine_failure(self) -> None:
        """Matrix C: EPERM + Jest genuine failure -> FAILED / TEST_FAILURE / eligible = False."""
        summary = CommandExecutionSummary(
            command="npm test",
            exit_code=1,
            categories=["TEST"],
            classification_text="EPERM: operation not permitted\nFAIL src/app.spec.ts\nTest Suites: 1 failed, 1 total\nTests: 1 failed, 1 total\n",
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "TEST_FAILURE")
        self.assertFalse(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_matrix_d_eperm_plus_typecheck_conclusive_error(self) -> None:
        """Matrix D: EPERM + TypeScript error -> FAILED / TYPECHECK_FAILURE / eligible = False."""
        summary = CommandExecutionSummary(
            command="npm run type-check",
            exit_code=1,
            categories=["TYPECHECK"],
            classification_text="EPERM: operation not permitted\nsrc/app.ts:14:5 - error TS2322: Type 'string' is not assignable to type 'number'.\nFound 1 error in src/app.ts:14\n",
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "TYPECHECK_FAILURE")
        self.assertFalse(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_matrix_e_eperm_plus_eslint_conclusive_failure(self) -> None:
        """Matrix E: EPERM + ESLint error -> FAILED / LINT_FAILURE / eligible = False."""
        summary = CommandExecutionSummary(
            command="npm run lint",
            exit_code=1,
            categories=["LINT"],
            classification_text="EPERM: operation not permitted\nsrc/app.ts\n  10:3  error  'foo' is defined but never used  @typescript-eslint/no-unused-vars\n\n✖ 1 problem (1 error, 0 warnings)\n",
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "LINT_FAILURE")
        self.assertFalse(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_matrix_f_eperm_plus_build_conclusive_failure(self) -> None:
        """Matrix F: EPERM + build failure -> FAILED / BUILD_FAILURE / eligible = False."""
        summary = CommandExecutionSummary(
            command="npm run build",
            exit_code=1,
            categories=["BUILD"],
            classification_text="EPERM: operation not permitted\nSyntaxError: Unexpected token (12:4)\nBuild failed with 1 error\n",
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "BUILD_FAILURE")
        self.assertFalse(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_matrix_g_fake_eperm_and_weak_fail_negative_controls(self) -> None:
        """
        Matrix G & Weak Negative Controls:
        Outputs containing 'FAIL-safe mode' or 'FAIL.log' or fake EPERM without conclusive
        project failure are classified as SANDBOX_DENIED (or blocked), requiring explicit human approval.
        """
        summary_weak = CommandExecutionSummary(
            command="npm test",
            exit_code=1,
            categories=["TEST"],
            classification_text="FAIL-safe mode initialized\nEPERM: operation not permitted, mkdir tmp\\jest\n",
        )
        outcome_weak = classify_command_outcome(summary_weak)
        self.assertEqual(outcome_weak.outcome, "BLOCKED")
        self.assertEqual(outcome_weak.reason_code, "SANDBOX_DENIED")
        self.assertTrue(is_verification_command_eligible(summary_weak, permissions=":read-only"))

    # =========================================================
    # 4. TRUE LATE-EVENT RECONCILIATION & DUPLICATE PROTECTION
    # =========================================================

    def test_true_late_event_offer_reconciliation(self) -> None:
        """
        Lifecycle: item/started -> item/completed(exit 1, empty output) -> late outputDelta(EPERM).
        After item/completed: offer count = 0 (inconclusive).
        After late outputDelta: re-evaluates -> outcome = SANDBOX_DENIED -> offer count = exactly 1.
        """
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        runner.current_permissions = ":read-only"

        offers_presented = []
        runner._safe_approval_prompt = lambda *args, **kwargs: offers_presented.append(kwargs) or "decline"

        result = TurnRunResult(thread_id="th-late-recon", turn_id="turn-late-recon")
        item_id = "cmd-late-recon"

        # 1. item/started
        runner._handle_notification(
            result,
            {"method": "item/started", "params": {"item": {"id": item_id, "type": "commandExecution", "command": "npm test"}}},
        )

        # 2. item/completed (exit 1, no inline output)
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

        # At this point, no offer was presented because output was empty
        self.assertEqual(len(offers_presented), 0)
        self.assertEqual(len(result.command_executions), 1)

        # 3. Late outputDelta arrives with EPERM
        runner._handle_notification(
            result,
            {
                "method": "item/commandExecution/outputDelta",
                "params": {"itemId": item_id, "delta": "EPERM: operation not permitted, mkdir tmp\\jest\n"},
            },
        )

        # Now offer was reconciled and presented exactly once
        self.assertEqual(len(offers_presented), 1)
        self.assertIn("Verification command requires writable runtime access", offers_presented[0].get("title", ""))

    def test_late_event_duplicate_delta_protection(self) -> None:
        """Sending repeated outputDelta chunks after offer presentation does NOT prompt again."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        runner.current_permissions = ":read-only"

        offers_presented = []
        runner._safe_approval_prompt = lambda *args, **kwargs: offers_presented.append(kwargs) or "decline"

        result = TurnRunResult(thread_id="th-late-dup", turn_id="turn-late-dup")
        item_id = "cmd-late-dup"

        runner._handle_notification(
            result,
            {"method": "item/started", "params": {"item": {"id": item_id, "type": "commandExecution", "command": "npm test"}}},
        )
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

        # First delta triggers offer
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": "EPERM: error 1\n"}},
        )
        self.assertEqual(len(offers_presented), 1)

        # Second delta MUST NOT trigger another offer
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": "EPERM: error 2\n"}},
        )
        # Third delta MUST NOT trigger another offer
        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": "more logs\n"}},
        )
        self.assertEqual(len(offers_presented), 1)

    def test_late_genuine_failure_no_offer(self) -> None:
        """Late outputDelta containing genuine test failure does NOT trigger an offer."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        runner.current_permissions = ":read-only"

        offers_presented = []
        runner._safe_approval_prompt = lambda *args, **kwargs: offers_presented.append(kwargs) or "decline"

        result = TurnRunResult(thread_id="th-late-fail", turn_id="turn-late-fail")
        item_id = "cmd-late-fail"

        runner._handle_notification(
            result,
            {"method": "item/started", "params": {"item": {"id": item_id, "type": "commandExecution", "command": "npm test"}}},
        )
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

        runner._handle_notification(
            result,
            {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": "FAIL src/test.ts\nTest Suites: 1 failed, 1 total\n"}},
        )

        self.assertEqual(len(offers_presented), 0)
        rec = result.command_executions[0]
        summary = CommandExecutionSummary(
            command=rec["command"],
            exit_code=rec["exit_code"],
            categories=rec["categories"],
            classification_text=rec["classification_text"],
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "TEST_FAILURE")

    # =========================================================
    # 5. INLINE aggregatedOutput QUALIFICATION
    # =========================================================

    def test_aggregated_output_small_and_large(self) -> None:
        """extract_command_diagnostic_text handles small, 512KB, and 5MB aggregatedOutput properly."""
        small_item = {"aggregatedOutput": "PASS all tests"}
        self.assertEqual(extract_command_diagnostic_text(small_item), "PASS all tests")

        big_start = {"aggregatedOutput": "EPERM: start error\n" + "X" * (5 * 1024 * 1024)}
        res_start = extract_command_diagnostic_text(big_start)
        self.assertIn("EPERM: start error", res_start)
        self.assertIn("[truncated", res_start)
        self.assertLessEqual(len(res_start.encode("utf-8")), MAX_COMMAND_OUTPUT_BYTES_RETAINED + 500)

        big_end = {"aggregatedOutput": "X" * (5 * 1024 * 1024) + "\nEPERM: operation not permitted, mkdir tmp\\jest\n"}
        res_end = extract_command_diagnostic_text(big_end)
        self.assertIn("EPERM: operation not permitted", res_end)
        self.assertIn("[truncated", res_end)
        self.assertLessEqual(len(res_end.encode("utf-8")), MAX_COMMAND_OUTPUT_BYTES_RETAINED + 500)

    # =========================================================
    # 6. MIXED INLINE + STREAM PRECEDENCE
    # =========================================================

    def test_mixed_inline_and_stream_precedence(self) -> None:
        """Documented precedence: non-empty inline fields take precedence over accumulated stream."""
        item1 = {"aggregatedOutput": "INLINE_AGG"}
        self.assertEqual(extract_command_diagnostic_text(item1, accumulated_stream="STREAM_TEXT"), "INLINE_AGG")

        item2 = {"output": "INLINE_OUT"}
        self.assertEqual(extract_command_diagnostic_text(item2, accumulated_stream="STREAM_TEXT"), "INLINE_OUT")

        item3 = {"error": "INLINE_ERR"}
        self.assertEqual(extract_command_diagnostic_text(item3, accumulated_stream="STREAM_TEXT"), "INLINE_ERR")

        item4 = {"stderr": "INLINE_STDERR"}
        self.assertEqual(extract_command_diagnostic_text(item4, accumulated_stream="STREAM_TEXT"), "INLINE_STDERR")

        item5 = {"id": "c1", "type": "commandExecution"}
        self.assertEqual(extract_command_diagnostic_text(item5, accumulated_stream="STREAM_USED"), "STREAM_USED")

        self.assertEqual(extract_command_diagnostic_text({}, accumulated_stream=""), "")

    # =========================================================
    # 7. INTERLEAVED COMMAND STRESS & ISOLATION (20 COMMANDS)
    # =========================================================

    def test_interleaved_command_stress_20_commands(self) -> None:
        """Simulate 20 command items with interleaved outputDelta notifications."""
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client=client, live=False)
        result = TurnRunResult(thread_id="th-stress", turn_id="turn-stress")

        num_commands = 20
        for chunk_idx in range(5):
            for cmd_idx in range(num_commands):
                item_id = f"cmd-stress-{cmd_idx}"
                delta = f"[CMD_{cmd_idx}_CHUNK_{chunk_idx}]\n"
                runner._handle_notification(
                    result,
                    {"method": "item/commandExecution/outputDelta", "params": {"itemId": item_id, "delta": delta}},
                )

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
            for other_idx in range(num_commands):
                if other_idx != cmd_idx:
                    self.assertNotIn(f"CMD_{other_idx}_", rec["classification_text"])
            for chunk_idx in range(5):
                self.assertIn(f"[CMD_{cmd_idx}_CHUNK_{chunk_idx}]", rec["classification_text"])

    # =========================================================
    # 8. MEMORY SOAK (100 MB SINGLE & MULTI-COMMAND)
    # =========================================================

    def test_memory_soak_100mb_single_command(self) -> None:
        """Push 100 MB through a single command accumulator and verify memory bound."""
        accum = BoundedDiagnosticAccumulator(max_total_bytes=512 * 1024, max_head_bytes=64 * 1024)

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
        chunk = "K" * (512 * 1024)
        for _ in range(20):
            for acc in accumulators:
                acc.push(chunk)

        for idx, acc in enumerate(accumulators):
            acc.push(f"\nEND_CMD_{idx}_EPERM\n")
            diag = acc.get_diagnostic_text()
            self.assertIn(f"END_CMD_{idx}_EPERM", diag)
            self.assertLessEqual(len(diag.encode("utf-8")), MAX_COMMAND_OUTPUT_BYTES_RETAINED + 500)


if __name__ == "__main__":
    unittest.main()
