from __future__ import annotations

from pathlib import Path
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

from verification_gate import (
    classify_file,
    determine_dominant_category,
    classify_command,
    unwrap_display_command,
    is_command_masked,
    CommandExecutionSummary,
    CommandOutcome,
    classify_command_outcome,
    AuditEvidenceAssessment,
    assess_read_only_audit,
    VerificationAssessment,
    assess_turn,
)


class TestVerificationGate(unittest.TestCase):

    def test_classify_source_code_files(self):
        self.assertEqual(classify_file("src/index.ts"), "SOURCE_CODE")
        self.assertEqual(classify_file("lib/util.py"), "SOURCE_CODE")
        self.assertEqual(classify_file("main.go"), "SOURCE_CODE")

    def test_classify_docs_only_files(self):
        self.assertEqual(classify_file("README.md"), "DOCS_ONLY")
        self.assertEqual(classify_file("docs/architecture.md"), "DOCS_ONLY")

    def test_dominant_category_resolution(self):
        categories = {"DOCS_ONLY", "SOURCE_CODE", "CONFIG_BUILD"}
        dominant = determine_dominant_category(categories)
        self.assertEqual(dominant, "SOURCE_CODE")

    def test_classify_commands(self):
        self.assertIn("TEST", classify_command("npm test"))
        self.assertIn("TEST", classify_command("pytest tests/"))
        self.assertIn("BUILD", classify_command("npm run build"))
        self.assertIn("LINT", classify_command("npm run lint"))
        self.assertIn("TYPECHECK", classify_command("npx tsc"))

    def test_masked_command_detection(self):
        self.assertTrue(is_command_masked("npm test || true"))
        self.assertTrue(is_command_masked("npm test ; exit 0"))
        self.assertFalse(is_command_masked("npm test"))

    # =========================================================================
    # FAZ 3: Command Outcome Classification Tests (FAILED vs BLOCKED)
    # =========================================================================

    def test_command_outcome_pass(self):
        """Exit code 0 and unmasked is PASSED."""
        cmd = CommandExecutionSummary(command="npm test", exit_code=0, categories=["TEST"])
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "PASSED")
        self.assertEqual(outcome.reason_code, "EXIT_SUCCESS")

    def test_command_outcome_true_test_failure(self):
        """Actual failing tests (exit 1 with test output) must be FAILED, NOT BLOCKED."""
        cmd = CommandExecutionSummary(
            command="npm test",
            exit_code=1,
            categories=["TEST"],
            output_snippet="FAIL src/app.test.ts\n  ✕ renders properly (12 ms)\n  2 failed, 10 passed",
        )
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "TEST_FAILURE")

    def test_command_outcome_sandbox_denial(self):
        """Sandbox / permission denial before running test must be BLOCKED."""
        cmd = CommandExecutionSummary(
            command="npm test",
            exit_code=1,
            categories=["TEST"],
            output_snippet="Error: spawn child_process EACCES: Access is denied",
        )
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "BLOCKED")
        self.assertEqual(outcome.reason_code, "SANDBOX_DENIED")

    def test_command_outcome_missing_executable(self):
        """Missing toolchain executable must be BLOCKED / EXECUTABLE_NOT_FOUND."""
        cmd = CommandExecutionSummary(
            command="go test ./...",
            exit_code=1,
            categories=["TEST"],
            output_snippet="'go' is not recognized as an internal or external command, operable program or batch file.",
        )
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "BLOCKED")
        self.assertEqual(outcome.reason_code, "EXECUTABLE_NOT_FOUND")

    def test_command_outcome_env_cache_init_failed(self):
        """Environment / build cache initialization failure must be BLOCKED."""
        cmd = CommandExecutionSummary(
            command="go test ./...",
            exit_code=1,
            categories=["TEST"],
            output_snippet="go: failed to initialize build cache at C:\\Users\\...: permission denied",
        )
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "BLOCKED")
        self.assertEqual(outcome.reason_code, "ENVIRONMENT_INIT_FAILED")

    def test_command_outcome_workspace_write_required(self):
        """Test runner attempting to write to read-only workspace is BLOCKED."""
        cmd = CommandExecutionSummary(
            command="pytest",
            exit_code=1,
            categories=["TEST"],
            output_snippet="[Errno 30] Read-only file system: '/workspace/.pytest_cache'",
        )
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "BLOCKED")
        self.assertEqual(outcome.reason_code, "WORKSPACE_WRITE_REQUIRED")

    def test_command_outcome_timeout(self):
        """Execution timeout is BLOCKED / TIMEOUT."""
        cmd = CommandExecutionSummary(
            command="npm test",
            exit_code=None,
            categories=["TEST"],
            output_snippet="Execution timeout exceeded (30000ms)",
        )
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "BLOCKED")
        self.assertEqual(outcome.reason_code, "TIMEOUT")

    # =========================================================================
    # FAZ 3: Read-Only Audit Assurance Matrix Tests
    # =========================================================================

    def test_audit_matrix_a_partial_with_blocked(self):
        """Scenario A: 1 pass + 2 blocked => PARTIAL."""
        cmds = [
            CommandExecutionSummary(command="npm audit", exit_code=0, categories=["TEST"]),
            CommandExecutionSummary(command="npm test", exit_code=1, categories=["TEST"], output_snippet="Access is denied"),
            CommandExecutionSummary(command="go test ./...", exit_code=1, categories=["TEST"], output_snippet="failed to initialize build cache"),
        ]
        audit = assess_read_only_audit(command_executions=cmds)
        self.assertEqual(audit.status, "PARTIAL")
        self.assertEqual(audit.passed_count, 1)
        self.assertEqual(audit.blocked_count, 2)

    def test_audit_matrix_b_complete_with_failed_test(self):
        """Scenario B: 1 pass + 1 fail => COMPLETE (failed test is conclusive finding!)."""
        cmds = [
            CommandExecutionSummary(command="npm test", exit_code=0, categories=["TEST"]),
            CommandExecutionSummary(command="go test ./...", exit_code=1, categories=["TEST"], output_snippet="FAIL calc_test.go"),
        ]
        audit = assess_read_only_audit(command_executions=cmds)
        self.assertEqual(audit.status, "COMPLETE")
        self.assertEqual(audit.passed_count, 1)
        self.assertEqual(audit.failed_count, 1)
        self.assertEqual(audit.blocked_count, 0)

    def test_audit_matrix_c_unverified_all_blocked(self):
        """Scenario C: 2 blocked => UNVERIFIED."""
        cmds = [
            CommandExecutionSummary(command="npm test", exit_code=1, categories=["TEST"], output_snippet="Access is denied"),
            CommandExecutionSummary(command="go test ./...", exit_code=1, categories=["TEST"], output_snippet="'go' is not recognized"),
        ]
        audit = assess_read_only_audit(command_executions=cmds)
        self.assertEqual(audit.status, "UNVERIFIED")
        self.assertEqual(audit.blocked_count, 2)
        self.assertEqual(audit.passed_count, 0)

    def test_audit_matrix_d_complete_all_passed(self):
        """Scenario D: 3 passed => COMPLETE."""
        cmds = [
            CommandExecutionSummary(command="npm audit", exit_code=0, categories=["TEST"]),
            CommandExecutionSummary(command="npm test", exit_code=0, categories=["TEST"]),
            CommandExecutionSummary(command="go test ./...", exit_code=0, categories=["TEST"]),
        ]
        audit = assess_read_only_audit(command_executions=cmds)
        self.assertEqual(audit.status, "COMPLETE")
        self.assertEqual(audit.passed_count, 3)

    def test_audit_matrix_e_complete_single_failed(self):
        """Scenario E: 1 failed test => COMPLETE (conclusive test failure report)."""
        cmds = [
            CommandExecutionSummary(command="npm test", exit_code=1, categories=["TEST"], output_snippet="3 failed, 5 passed"),
        ]
        audit = assess_read_only_audit(command_executions=cmds)
        self.assertEqual(audit.status, "COMPLETE")
        self.assertEqual(audit.failed_count, 1)

    def test_audit_matrix_f_interrupted(self):
        """Scenario F: Turn interrupted => INTERRUPTED."""
        cmds = [
            CommandExecutionSummary(command="npm test", exit_code=0, categories=["TEST"]),
        ]
        audit = assess_read_only_audit(command_executions=cmds, is_interrupted=True)
        self.assertEqual(audit.status, "INTERRUPTED")

    # =========================================================================
    # FAZ 3: Post-Mutation Verification Matrix Tests
    # =========================================================================

    def test_post_mutation_verified(self):
        """Source changed + test pass => VERIFIED."""
        assessment = assess_turn(
            changed_files=["src/app.py"],
            command_executions=[CommandExecutionSummary(command="pytest", exit_code=0, categories=["TEST"], sequence=2)],
            last_mutation_seq=1,
            is_continuation=True,
        )
        self.assertEqual(assessment.status, "VERIFIED")
        self.assertEqual(assessment.evidence_level, "STRONG")

    def test_post_mutation_failed_test(self):
        """Source changed + test fail (actual failing test) => FAILED, NOT BLOCKED."""
        assessment = assess_turn(
            changed_files=["src/app.py"],
            command_executions=[
                CommandExecutionSummary(
                    command="pytest",
                    exit_code=1,
                    categories=["TEST"],
                    sequence=2,
                    output_snippet="FAILED test_app.py::test_login - AssertionError",
                )
            ],
            last_mutation_seq=1,
            is_continuation=True,
        )
        self.assertEqual(assessment.status, "FAILED")
        self.assertEqual(assessment.reason, "TEST_FAILED_AFTER_CONTINUATION")

    def test_post_mutation_blocked_sandbox(self):
        """Source changed + test blocked due to sandbox => BLOCKED."""
        assessment = assess_turn(
            changed_files=["src/app.py"],
            command_executions=[
                CommandExecutionSummary(
                    command="pytest",
                    exit_code=1,
                    categories=["TEST"],
                    sequence=2,
                    output_snippet="Access is denied: spawn process",
                )
            ],
            last_mutation_seq=1,
            is_continuation=True,
        )
        self.assertEqual(assessment.status, "BLOCKED")
        self.assertEqual(assessment.reason, "VERIFICATION_BLOCKED_AFTER_CONTINUATION")

    def test_post_mutation_static_pass_and_tests_blocked(self):
        """Source changed + lint pass + test blocked => PARTIALLY_VERIFIED."""
        assessment = assess_turn(
            changed_files=["src/app.py"],
            command_executions=[
                CommandExecutionSummary(command="npx tsc", exit_code=0, categories=["TYPECHECK"], sequence=2),
                CommandExecutionSummary(command="npm test", exit_code=1, categories=["TEST"], sequence=3, output_snippet="Access is denied"),
            ],
            last_mutation_seq=1,
            is_continuation=True,
        )
        self.assertEqual(assessment.status, "PARTIALLY_VERIFIED")

    def test_post_mutation_docs_only_not_applicable(self):
        """Docs-only mutation => NOT_APPLICABLE."""
        assessment = assess_turn(
            changed_files=["README.md"],
            command_executions=[],
            last_mutation_seq=1,
            is_continuation=False,
        )
        self.assertEqual(assessment.status, "NOT_APPLICABLE")

    def test_post_mutation_user_skip(self):
        """User explicitly requested skip => UNVERIFIED."""
        assessment = assess_turn(
            changed_files=["src/app.py"],
            command_executions=[],
            last_mutation_seq=1,
            is_continuation=False,
            user_skip=True,
        )
        self.assertEqual(assessment.status, "UNVERIFIED")
        self.assertEqual(assessment.reason, "USER_REQUESTED_SKIP")

    # =========================================================================
    # FAZ 3: Final Adversarial Tests (Fallback & Precision)
    # =========================================================================

    def test_adversarial_a_custom_test_runner_inconclusive(self):
        """Case A: Unknown error output on non-zero exit => INCONCLUSIVE."""
        cmd = CommandExecutionSummary(
            command="custom-test-runner",
            exit_code=1,
            categories=["TEST"],
            output_snippet="unexpected backend error",
        )
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "INCONCLUSIVE")
        self.assertEqual(outcome.reason_code, "INCONCLUSIVE_NON_ZERO_EXIT")

    def test_adversarial_b_npm_test_infrastructure_error_inconclusive(self):
        """Case B: npm test with infrastructure failure (no test results) => INCONCLUSIVE."""
        cmd = CommandExecutionSummary(
            command="npm test",
            exit_code=1,
            categories=["TEST"],
            output_snippet="custom infrastructure backend unavailable",
        )
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "INCONCLUSIVE")
        self.assertEqual(outcome.reason_code, "INCONCLUSIVE_NON_ZERO_EXIT")

    def test_adversarial_c_npm_test_conclusive_failure(self):
        """Case C: npm test with conclusive failing test report => FAILED / TEST_FAILURE."""
        cmd = CommandExecutionSummary(
            command="npm test",
            exit_code=1,
            categories=["TEST"],
            output_snippet="FAIL src/app.test.ts\n2 failed, 10 passed",
        )
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "TEST_FAILURE")

    def test_adversarial_d_go_test_conclusive_failure(self):
        """Case D: go test with conclusive FAIL marker => FAILED / TEST_FAILURE."""
        cmd = CommandExecutionSummary(
            command="go test ./...",
            exit_code=1,
            categories=["TEST"],
            output_snippet="FAIL example/pkg",
        )
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "FAILED")
        self.assertEqual(outcome.reason_code, "TEST_FAILURE")

    def test_adversarial_e_npm_test_sandbox_denied(self):
        """Case E: npm test with sandbox / permission denial => BLOCKED / SANDBOX_DENIED."""
        cmd = CommandExecutionSummary(
            command="npm test",
            exit_code=1,
            categories=["TEST"],
            output_snippet="Access is denied",
        )
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "BLOCKED")
        self.assertEqual(outcome.reason_code, "SANDBOX_DENIED")

    def test_adversarial_f_raw_classification_text_before_truncation(self):
        """Case F: Classification inspects full text before 500-char snippet truncation."""
        full_text = "A" * 550 + "\nError: spawn child_process: Access is denied"
        cmd = CommandExecutionSummary(
            command="npm test",
            exit_code=1,
            categories=["TEST"],
            output_snippet=full_text[:500],
            classification_text=full_text,
        )
        outcome = classify_command_outcome(cmd)
        self.assertEqual(outcome.outcome, "BLOCKED")
        self.assertEqual(outcome.reason_code, "SANDBOX_DENIED")

    def test_audit_aggregation_passed_plus_inconclusive(self):
        """Audit: 1 PASSED + 1 INCONCLUSIVE => PARTIAL."""
        cmds = [
            CommandExecutionSummary(command="npm test", exit_code=0, categories=["TEST"]),
            CommandExecutionSummary(command="custom-test-runner", exit_code=1, categories=["TEST"], output_snippet="unexpected backend error"),
        ]
        audit = assess_read_only_audit(command_executions=cmds)
        self.assertEqual(audit.status, "PARTIAL")
        self.assertEqual(audit.passed_count, 1)
        self.assertEqual(audit.inconclusive_count, 1)

    def test_audit_aggregation_inconclusive_only(self):
        """Audit: INCONCLUSIVE only => UNVERIFIED."""
        cmds = [
            CommandExecutionSummary(command="custom-test-runner", exit_code=1, categories=["TEST"], output_snippet="unexpected backend error"),
        ]
        audit = assess_read_only_audit(command_executions=cmds)
        self.assertEqual(audit.status, "UNVERIFIED")
        self.assertEqual(audit.inconclusive_count, 1)

    def test_post_mutation_inconclusive_command(self):
        """Post-mutation: Inconclusive command in continuation => UNVERIFIED / INCONCLUSIVE_VERIFICATION."""
        assessment = assess_turn(
            changed_files=["src/app.py"],
            command_executions=[
                CommandExecutionSummary(
                    command="npm test",
                    exit_code=1,
                    categories=["TEST"],
                    sequence=2,
                    output_snippet="custom infrastructure backend unavailable",
                )
            ],
            last_mutation_seq=1,
            is_continuation=True,
        )
        self.assertEqual(assessment.status, "UNVERIFIED")
        self.assertEqual(assessment.reason, "INCONCLUSIVE_VERIFICATION")


if __name__ == "__main__":
    unittest.main()
