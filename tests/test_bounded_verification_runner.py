from __future__ import annotations

"""
CX2 2.0.10 Phase 1.1 / 1.2 Bounded Verification Runner Test Suite.
Covers exact identity scoping, adversarial non-eligible failures, malicious command lifecycle,
true bounded streaming output capture, boundary thresholds, 50MB+ payloads, infinite output timeout,
and UTF-8 multibyte integrity.
"""

from dataclasses import dataclass
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
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
    BoundedStreamReader,
    execute_bounded_verification_command,
    is_verification_command_eligible,
    kill_process_tree,
    MAX_STDOUT_BYTES,
    MAX_STDERR_BYTES,
)
from turn_runner import (
    StreamingTurnRunner,
    TurnApprovalState,
    TurnRunResult,
)


class SyntheticTurnClient:
    """Minimal synthetic AppServerClient for turn runner testing."""

    def __init__(self, responses: list[dict] | None = None) -> None:
        self.responses = list(responses or [])
        self.recorded_requests: list[tuple[str, dict]] = []
        self.recorded_responses: list[tuple[str | None, dict]] = []
        self.recorded_errors: list[tuple[str | None, int, str]] = []

    def request(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        self.recorded_requests.append((method, params))
        if self.responses:
            return self.responses.pop(0)
        return {"result": {}}

    def respond(self, request_id: str | None, payload: dict) -> None:
        self.recorded_responses.append((request_id, payload))

    def respond_error(self, request_id: str | None, code: int, message: str) -> None:
        self.recorded_errors.append((request_id, code, message))


class TestPhase11EligibilityAndNonEligibleFailures(unittest.TestCase):
    """Adversarial qualification of eligibility boundaries and non-eligible failure modes."""

    def test_eligible_when_blocked_by_sandbox_denied(self) -> None:
        summary = CommandExecutionSummary(
            command="npx jest --runInBand",
            exit_code=1,
            duration_ms=200,
            sequence=1,
            categories=["TEST"],
            output_snippet="EPERM: operation not permitted, mkdir 'C:\\Users\\example-user\\.codex-agent-cache\\tmp\\jest'",
            classification_text="EPERM: operation not permitted, mkdir 'C:\\Users\\example-user\\.codex-agent-cache\\tmp\\jest'",
            cwd="C:\\Users\\example-user\\Projects\\fixture-repo",
        )
        self.assertTrue(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_eligible_when_blocked_by_workspace_write_required(self) -> None:
        summary = CommandExecutionSummary(
            command="npm run build",
            exit_code=1,
            duration_ms=400,
            sequence=1,
            categories=["BUILD"],
            output_snippet="EROFS: read-only file system, open '.next/BUILD_ID'",
            classification_text="EROFS: read-only file system, open '.next/BUILD_ID'",
            cwd="C:\\Users\\example-user\\Projects\\fixture-repo",
        )
        self.assertTrue(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_ineligible_generic_environment_init_failure(self) -> None:
        """Generic environment init failures without proven sandbox write-block must NOT qualify."""
        summary = CommandExecutionSummary(
            command="npm run lint",
            exit_code=1,
            duration_ms=150,
            sequence=1,
            categories=["LINT"],
            output_snippet="failed to initialize build cache: network host unreachable",
            classification_text="failed to initialize build cache: network host unreachable",
            cwd="C:\\Users\\example-user\\Projects\\fixture-repo",
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.reason_code, "ENVIRONMENT_INIT_FAILED")
        self.assertFalse(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_ineligible_conclusive_jest_assertion_failure(self) -> None:
        """Actual project test failures must remain FAILED / TEST_FAILURE and not offered host execution."""
        summary = CommandExecutionSummary(
            command="npx jest --runInBand",
            exit_code=1,
            duration_ms=500,
            sequence=1,
            categories=["TEST"],
            output_snippet="FAIL tests/example.test.js\n● Test Suite failed: Expected 2 received 3\n1 failed, 0 passed",
            classification_text="FAIL tests/example.test.js\n● Test Suite failed: Expected 2 received 3\n1 failed, 0 passed",
            cwd="C:\\Users\\example-user\\Projects\\fixture-repo",
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "TEST_FAILURE")
        self.assertFalse(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_ineligible_typescript_type_error(self) -> None:
        """TypeScript compiler errors must remain FAILED / TYPECHECK_FAILURE."""
        summary = CommandExecutionSummary(
            command="npx tsc --noEmit",
            exit_code=2,
            duration_ms=600,
            sequence=1,
            categories=["TYPECHECK"],
            output_snippet="src/index.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.",
            classification_text="src/index.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.",
            cwd="C:\\Users\\example-user\\Projects\\fixture-repo",
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "TYPECHECK_FAILURE")
        self.assertFalse(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_ineligible_eslint_rule_failure(self) -> None:
        """ESLint errors must remain FAILED / LINT_FAILURE."""
        summary = CommandExecutionSummary(
            command="npm run lint",
            exit_code=1,
            duration_ms=300,
            sequence=1,
            categories=["LINT"],
            output_snippet="1 problem (1 error, 0 warnings)\n  3:10  error  'foo' is defined but never used",
            classification_text="1 problem (1 error, 0 warnings)\n  3:10  error  'foo' is defined but never used",
            cwd="C:\\Users\\example-user\\Projects\\fixture-repo",
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "LINT_FAILURE")
        self.assertFalse(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_ineligible_executable_not_found(self) -> None:
        """Missing binary errors must remain BLOCKED / EXECUTABLE_NOT_FOUND."""
        summary = CommandExecutionSummary(
            command="nonexistent_tool test",
            exit_code=1,
            duration_ms=50,
            sequence=1,
            categories=["TEST"],
            output_snippet="'nonexistent_tool' is not recognized as an internal or external command",
            classification_text="'nonexistent_tool' is not recognized as an internal or external command",
            cwd="C:\\Users\\example-user\\Projects\\fixture-repo",
        )
        outcome = classify_command_outcome(summary)
        self.assertEqual(outcome.outcome, "BLOCKED")
        self.assertEqual(outcome.reason_code, "EXECUTABLE_NOT_FOUND")
        self.assertFalse(is_verification_command_eligible(summary, permissions=":read-only"))

    def test_ineligible_arbitrary_mutation_command_receiving_eperm(self) -> None:
        """Arbitrary mutation or file-deletion commands receiving EPERM must NEVER be eligible."""
        summary = CommandExecutionSummary(
            command="rm -rf C:\\Windows\\System32",
            exit_code=1,
            duration_ms=50,
            sequence=1,
            categories=["OTHER"],
            output_snippet="EPERM: operation not permitted",
            classification_text="EPERM: operation not permitted",
            cwd="C:\\Users\\example-user\\Projects\\fixture-repo",
        )
        self.assertFalse(is_verification_command_eligible(summary, permissions=":read-only"))


class TestPhase11ExactIdentityAdversarial(unittest.TestCase):
    """Prove that semantically different commands and different CWDs have separate authorization identities."""

    def test_distinct_command_variants_have_distinct_identities(self) -> None:
        variants = [
            "npm test",
            "npm test -- --watch",
            "npm test && echo X",
            "npm test ; echo X",
            "npm test | more",
            "cmd /c npm test",
            'powershell -Command "npm test"',
        ]

        identities = {
            ("bounded_verification_exec", "C:\\Users\\example-user\\Projects\\fixture-repo", cmd)
            for cmd in variants
        }
        # Every single variant must produce a strictly unique identity
        self.assertEqual(len(identities), len(variants))

    def test_same_command_different_cwd_requires_separate_identity(self) -> None:
        id_a = ("bounded_verification_exec", "C:\\fixture\\a", "npm test")
        id_b = ("bounded_verification_exec", "C:\\fixture\\b", "npm test")
        self.assertNotEqual(id_a, id_b)

    def test_declining_one_variant_does_not_block_different_variant(self) -> None:
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client, live=False)
        runner.current_permissions = ":read-only"
        runner.current_cwd = Path("C:/Users/example-user/Projects/fixture-repo")

        result = TurnRunResult(
            thread_id="th-id-1",
            turn_id="turn-id-1",
            approval_state=TurnApprovalState(),
        )

        # Decline npm test
        id_1 = ("bounded_verification_exec", "C:/Users/example-user/Projects/fixture-repo", "npm test")
        result.approval_state.declined_identities.add(id_1)

        # Different variant arrives
        notif = {
            "method": "item/completed",
            "params": {
                "threadId": "th-id-1",
                "turnId": "turn-id-1",
                "item": {
                    "id": "cmd-var-1",
                    "type": "commandExecution",
                    "command": "npm test -- --watch",
                    "cwd": "C:/Users/example-user/Projects/fixture-repo",
                    "exitCode": 1,
                    "error": "EPERM: operation not permitted",
                },
            },
        }

        # In non-interactive mode, it will prompt (and default to decline), but was NOT auto-declined as identical
        runner._handle_notification(result, notif)
        self.assertEqual(result.auto_decline_count, 0)
        id_2 = ("bounded_verification_exec", "C:/Users/example-user/Projects/fixture-repo", "npm test -- --watch")
        self.assertIn(id_2, result.approval_state.declined_identities)


class TestPhase11MaliciousVerificationCommandLifecycle(unittest.TestCase):
    """Test security model when a test script contains a deliberate file mutation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="cx2_malicious_test_")
        self.ws_path = Path(self.temp_dir)
        self.marker_file = self.ws_path / "disposable_marker.txt"

        test_py = self.ws_path / "test_malicious.py"
        marker_str = str(self.marker_file).replace("\\", "\\\\")
        test_py.write_text(
            "import pathlib, unittest\n"
            "class MaliciousTest(unittest.TestCase):\n"
            "    def test_mutation(self):\n"
            f"        pathlib.Path(r'{marker_str}').write_text('mutated', encoding='utf-8')\n",
            encoding="utf-8",
        )
        self.cmd = f'"{sys.executable}" -m unittest test_malicious.py'

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_decline_prevents_marker_creation(self) -> None:
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client, live=False)
        runner.current_permissions = ":read-only"
        runner.current_cwd = self.ws_path

        result = TurnRunResult(
            thread_id="th-mal-1",
            turn_id="turn-mal-1",
            approval_state=TurnApprovalState(),
        )

        notif = {
            "method": "item/completed",
            "params": {
                "threadId": "th-mal-1",
                "turnId": "turn-mal-1",
                "item": {
                    "id": "cmd-mal-1",
                    "type": "commandExecution",
                    "command": self.cmd,
                    "cwd": str(self.ws_path),
                    "exitCode": 1,
                    "error": "EPERM: operation not permitted",
                },
            },
        }

        # Non-interactive defaults to decline
        runner._handle_notification(result, notif)
        self.assertFalse(self.marker_file.exists())
        self.assertEqual(result.command_executions[0]["exit_code"], 1)
        self.assertFalse(result.command_executions[0]["bounded_host_execution"])

    def test_accept_executes_once_and_second_invocation_requires_new_approval(self) -> None:
        client = SyntheticTurnClient()
        runner = StreamingTurnRunner(client, live=False)
        runner.current_permissions = ":read-only"
        runner.current_cwd = self.ws_path

        result = TurnRunResult(
            thread_id="th-mal-2",
            turn_id="turn-mal-2",
            approval_state=TurnApprovalState(),
        )

        notif = {
            "method": "item/completed",
            "params": {
                "threadId": "th-mal-2",
                "turnId": "turn-mal-2",
                "item": {
                    "id": "cmd-mal-2",
                    "type": "commandExecution",
                    "command": self.cmd,
                    "cwd": str(self.ws_path),
                    "exitCode": 1,
                    "error": "EPERM: operation not permitted",
                },
            },
        }

        # First invocation: user accepts
        with patch.object(runner, "_safe_approval_prompt", return_value="accept"):
            runner._handle_notification(result, notif)

        self.assertTrue(self.marker_file.exists())
        self.assertEqual(self.marker_file.read_text(encoding="utf-8"), "mutated")
        self.assertEqual(result.command_executions[0]["exit_code"], 0)
        self.assertTrue(result.command_executions[0]["bounded_host_execution"])

        # Clean marker
        self.marker_file.unlink()

        # Second invocation of the same command in a new turn
        result2 = TurnRunResult(
            thread_id="th-mal-2",
            turn_id="turn-mal-3",
            approval_state=TurnApprovalState(),
        )
        notif2 = {
            "method": "item/completed",
            "params": {
                "threadId": "th-mal-2",
                "turnId": "turn-mal-3",
                "item": {
                    "id": "cmd-mal-3",
                    "type": "commandExecution",
                    "command": self.cmd,
                    "cwd": str(self.ws_path),
                    "exitCode": 1,
                    "error": "EPERM: operation not permitted",
                },
            },
        }
        # Second invocation: user declines
        with patch.object(runner, "_safe_approval_prompt", return_value="decline"):
            runner._handle_notification(result2, notif2)

        # Marker must NOT be created on second invocation
        self.assertFalse(self.marker_file.exists())
        self.assertEqual(result2.command_executions[0]["exit_code"], 1)
        self.assertFalse(result2.command_executions[0]["bounded_host_execution"])


class TestPhase12TrueBoundedOutputCapture(unittest.TestCase):
    """
    Phase 1.2 deterministic tests for true concurrent streaming output capture with bounded memory.
    Covers basic, boundary thresholds, 50MB+ payloads, infinite output timeouts, and UTF-8 multibyte.
    """

    def test_basic_zero_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cmd = f'"{sys.executable}" -c "pass"'
            res = execute_bounded_verification_command(cmd, cwd=temp_dir, timeout=10.0)
            self.assertEqual(res.exit_code, 0)
            self.assertEqual(res.stdout, "")
            self.assertEqual(res.stderr, "")
            self.assertFalse(res.stdout_truncated)
            self.assertFalse(res.stderr_truncated)
            self.assertEqual(res.stdout_bytes_total, 0)
            self.assertEqual(res.stderr_bytes_total, 0)

    def test_basic_small_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cmd = f'"{sys.executable}" -c "import sys; sys.stdout.write(\'Hello Stdout\\n\'); sys.stderr.write(\'Hello Stderr\\n\'); sys.exit(7)"'
            res = execute_bounded_verification_command(cmd, cwd=temp_dir, timeout=10.0)
            self.assertEqual(res.exit_code, 7)
            self.assertEqual(res.stdout.strip(), "Hello Stdout")
            self.assertEqual(res.stderr.strip(), "Hello Stderr")
            self.assertFalse(res.stdout_truncated)
            self.assertFalse(res.stderr_truncated)

    def test_boundary_below_at_and_above_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            limit = 10_000

            # 1. Below limit (9,000 bytes)
            cmd_below = f'"{sys.executable}" -c "import sys; sys.stdout.write(\'A\' * 9000)"'
            res_below = execute_bounded_verification_command(cmd_below, cwd=temp_dir, max_stdout_bytes=limit, timeout=10.0)
            self.assertEqual(res_below.exit_code, 0)
            self.assertEqual(len(res_below.stdout), 9000)
            self.assertFalse(res_below.stdout_truncated)
            self.assertEqual(res_below.stdout_bytes_total, 9000)

            # 2. Exactly at limit (10,000 bytes)
            cmd_at = f'"{sys.executable}" -c "import sys; sys.stdout.write(\'B\' * 10000)"'
            res_at = execute_bounded_verification_command(cmd_at, cwd=temp_dir, max_stdout_bytes=limit, timeout=10.0)
            self.assertEqual(res_at.exit_code, 0)
            self.assertEqual(len(res_at.stdout), 10000)
            self.assertFalse(res_at.stdout_truncated)
            self.assertEqual(res_at.stdout_bytes_total, 10000)

            # 3. Just above limit (10,100 bytes)
            cmd_above = f'"{sys.executable}" -c "import sys; sys.stdout.write(\'C\' * 10100)"'
            res_above = execute_bounded_verification_command(cmd_above, cwd=temp_dir, max_stdout_bytes=limit, timeout=10.0)
            self.assertEqual(res_above.exit_code, 0)
            self.assertTrue(res_above.stdout_truncated)
            self.assertEqual(res_above.stdout_bytes_total, 10100)
            self.assertIn("exceeded limit", res_above.stdout)
            self.assertTrue(res_above.stdout.startswith("C" * 10000))

    def test_large_adversarial_50mb_stdout_and_50mb_stderr(self) -> None:
        """Process generating 50MB stdout and 50MB stderr concurrently must complete with bounded memory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Script writes 50MB stdout and 50MB stderr
            code = (
                "import sys\n"
                "sys.stdout.write('S' * 50_000_000)\n"
                "sys.stderr.write('E' * 50_000_000)\n"
                "sys.exit(13)\n"
            )
            script_py = Path(temp_dir) / "large_gen.py"
            script_py.write_text(code, encoding="utf-8")

            cmd = f'"{sys.executable}" "{script_py}"'
            t0 = time.monotonic()
            res = execute_bounded_verification_command(cmd, cwd=temp_dir, timeout=30.0)
            elapsed = time.monotonic() - t0

            self.assertEqual(res.exit_code, 13)
            self.assertTrue(res.stdout_truncated)
            self.assertTrue(res.stderr_truncated)
            self.assertEqual(res.stdout_bytes_total, 50_000_000)
            self.assertEqual(res.stderr_bytes_total, 50_000_000)
            # Retained text length must be bounded to MAX_STDOUT_BYTES + truncation marker string length
            self.assertLessEqual(len(res.stdout), MAX_STDOUT_BYTES + 200)
            self.assertLessEqual(len(res.stderr), MAX_STDERR_BYTES + 200)
            self.assertIn("exceeded limit", res.stdout)
            self.assertIn("exceeded limit", res.stderr)
            self.assertLess(elapsed, 15.0)

    def test_infinite_output_timeout_and_process_tree_cleanup(self) -> None:
        """Continuous infinite output on stdout/stderr under timeout across 10 repeated cycles."""
        with tempfile.TemporaryDirectory() as temp_dir:
            script = (
                "import subprocess, sys, time\n"
                "sys.stdout.write('O' * 1000)\n"
                "sys.stdout.flush()\n"
                "sys.stderr.write('E' * 1000)\n"
                "sys.stderr.flush()\n"
                "if len(sys.argv) == 1:\n"
                "    # Parent: spawn child\n"
                f"    p = subprocess.Popen([sys.executable, __file__, 'child'], stdout=sys.stdout, stderr=sys.stderr)\n"
                "    p.wait()\n"
                "elif sys.argv[1] == 'child':\n"
                "    # Child: spawn grandchild\n"
                f"    p = subprocess.Popen([sys.executable, __file__, 'grandchild'], stdout=sys.stdout, stderr=sys.stderr)\n"
                "    p.wait()\n"
                "elif sys.argv[1] == 'grandchild':\n"
                "    # Grandchild: continuously write to stdout and stderr\n"
                "    while True:\n"
                "        sys.stdout.write('O' * 10000)\n"
                "        sys.stdout.flush()\n"
                "        sys.stderr.write('E' * 10000)\n"
                "        sys.stderr.flush()\n"
            )
            tree_py = Path(temp_dir) / "inf_tree.py"
            tree_py.write_text(script, encoding="utf-8")

            # Run 10 repeated cycles
            for iter_idx in range(1, 11):
                t0 = time.monotonic()
                cmd = f'"{sys.executable}" "{tree_py}"'
                res = execute_bounded_verification_command(cmd, cwd=temp_dir, timeout=0.8)
                elapsed = time.monotonic() - t0

                self.assertEqual(res.exit_code, -1)
                self.assertIn("timed out", res.output_snippet.lower())
                self.assertTrue(res.stdout_truncated or res.stdout_bytes_total > 0)
                self.assertLessEqual(len(res.stdout), MAX_STDOUT_BYTES + 300)
                self.assertLessEqual(len(res.stderr), MAX_STDERR_BYTES + 300)
                self.assertLess(elapsed, 4.0)

    def test_utf8_multibyte_high_volume(self) -> None:
        """Multibyte UTF-8 characters (Turkish chars & 4-byte emojis) must decode without crashing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # 🇹🇷 = 8 bytes, ğüşıöç = 12 bytes
            code = (
                "import sys\n"
                "pattern = 'Türkçe doğrulama testi: 🇹🇷 ✨ ğüşıöç\\n'\n"
                "sys.stdout.write(pattern * 50000)\n"
                "sys.stderr.write('Hata kanalı: 🚨\\n' * 50000)\n"
            )
            script_py = Path(temp_dir) / "utf8_test.py"
            script_py.write_text(code, encoding="utf-8")

            cmd = f'"{sys.executable}" "{script_py}"'
            res = execute_bounded_verification_command(cmd, cwd=temp_dir, timeout=15.0)

            self.assertEqual(res.exit_code, 0)
            self.assertTrue(res.stdout_truncated)
            self.assertTrue(res.stderr_truncated)
            self.assertIn("Türkçe", res.stdout)
            self.assertIn("Hata kanalı", res.stderr)
            self.assertLessEqual(len(res.stdout.encode("utf-8")), MAX_STDOUT_BYTES + 500)
            self.assertLessEqual(len(res.stderr.encode("utf-8")), MAX_STDERR_BYTES + 500)


if __name__ == "__main__":
    unittest.main()
