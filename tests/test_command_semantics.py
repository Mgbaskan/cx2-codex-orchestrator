from __future__ import annotations

import io
from pathlib import Path
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

sys.path.insert(0, _bootstrap.RUNTIME_DIR)

from required_verification import (
    RequiredVerificationPlan,
    extract_required_verification_plan,
)
from terminal_ui import TerminalRenderer
from verification_gate import (
    CommandExecutionSummary,
    classify_command,
    classify_command_outcome,
    is_ripgrep_command,
    unwrap_display_command,
)


class TestCommandSemantics(unittest.TestCase):

    # =============================================================
    # 1. F-005B: Unwrapping cmd /c and wrapper recursion
    # =============================================================

    def test_cmd_unquoted_unwrap(self) -> None:
        """cmd /c unquoted command is unwrapped cleanly."""
        self.assertEqual(unwrap_display_command("cmd /c git status"), "git status")
        self.assertEqual(unwrap_display_command("cmd.exe /c git status"), "git status")
        self.assertEqual(unwrap_display_command("cmd /c echo hello world"), "echo hello world")

    def test_cmd_quoted_unwrap_regression(self) -> None:
        """cmd /c quoted forms continue to unwrap cleanly."""
        self.assertEqual(unwrap_display_command('cmd /c "git status"'), "git status")
        self.assertEqual(unwrap_display_command('cmd.exe /c "npm run type-check"'), "npm run type-check")

    def test_nested_wrapper_recursion(self) -> None:
        """Nested powershell and cmd wrappers unwrap with bounded recursion."""
        nested = 'powershell -Command "cmd /c rg pattern ."'
        self.assertEqual(unwrap_display_command(nested), "rg pattern .")

    def test_cmd_edge_cases(self) -> None:
        """cmd /c edge cases fail conservative and preserve syntax."""
        self.assertEqual(unwrap_display_command("cmd /c"), "cmd /c")
        self.assertEqual(
            unwrap_display_command('cmd /c "C:\\Program Files\\Tool\\tool.exe" arg'),
            '"C:\\Program Files\\Tool\\tool.exe" arg',
        )

    # =============================================================
    # 2. F-003B: Type-check classification
    # =============================================================

    def test_typecheck_patterns(self) -> None:
        """npm/pnpm/yarn/bun type-check variants are classified as TYPECHECK."""
        self.assertIn("TYPECHECK", classify_command("npm run typecheck"))
        self.assertIn("TYPECHECK", classify_command("npm run type-check"))
        self.assertIn("TYPECHECK", classify_command("pnpm run type-check"))
        self.assertIn("TYPECHECK", classify_command("pnpm type-check"))
        self.assertIn("TYPECHECK", classify_command("yarn type-check"))
        self.assertIn("TYPECHECK", classify_command("bun type-check"))

    def test_typecheck_negative_controls(self) -> None:
        """Non-exact script names do not accidentally match TYPECHECK."""
        self.assertNotIn("TYPECHECK", classify_command("npm run type-check-docs"))
        self.assertNotIn("TYPECHECK", classify_command("npm run typecheck-all-docs"))
        self.assertNotIn("TYPECHECK", classify_command("npm run check-types"))

    def test_required_verification_typecheck_interaction(self) -> None:
        """Required verification accepts npm run type-check exit 0 and rejects nonzero."""
        plan_text = """
### QUALITY GATES
Web:
- npm run type-check
"""
        plan = extract_required_verification_plan(plan_text)
        self.assertTrue(len(plan.gates) >= 1)

        # Exit code 0 -> PASSED
        cmd_summary_pass = CommandExecutionSummary(
            command="npm run type-check",
            display_command="npm run type-check",
            categories=["TYPECHECK"],
            exit_code=0,
            duration_ms=200,
            sequence=1,
            is_masked=False,
            output_snippet="",
            classification_text="",
            cwd=str(REPO_ROOT / "web"),
        )
        outcome_pass = classify_command_outcome(cmd_summary_pass)
        self.assertEqual(outcome_pass.outcome, "PASSED")

        # Exit code 1 -> FAILED / INCONCLUSIVE (not passed!)
        cmd_summary_fail = CommandExecutionSummary(
            command="npm run type-check",
            display_command="npm run type-check",
            categories=["TYPECHECK"],
            exit_code=1,
            duration_ms=200,
            sequence=2,
            is_masked=False,
            output_snippet="Found 2 errors",
            classification_text="Found 2 errors",
            cwd=str(REPO_ROOT / "web"),
        )
        outcome_fail = classify_command_outcome(cmd_summary_fail)
        self.assertNotEqual(outcome_fail.outcome, "PASSED")

    # =============================================================
    # 3. F-003A: Ripgrep UI presentation & semantic separation
    # =============================================================

    def test_is_ripgrep_command(self) -> None:
        """is_ripgrep_command identifies direct and wrapped rg commands."""
        self.assertTrue(is_ripgrep_command("rg pattern file.txt"))
        self.assertTrue(is_ripgrep_command("rg.exe -i needle"))
        self.assertTrue(is_ripgrep_command('cmd /c "rg foo ."'))
        self.assertTrue(is_ripgrep_command('powershell -Command "rg.exe bar"'))
        self.assertFalse(is_ripgrep_command("git status"))
        self.assertFalse(is_ripgrep_command("npm test"))
        self.assertFalse(is_ripgrep_command("jest --testMatch"))

    def test_ripgrep_ui_exit_0_success(self) -> None:
        """rg with match (exit 0) displays [ok] badge."""
        out = io.StringIO()
        ui = TerminalRenderer(stream=out)
        ui.command_completed({
            "command": "rg needle file.txt",
            "exitCode": 0,
            "durationMs": 30,
        })
        text = out.getvalue()
        self.assertIn("[ok]", text)
        self.assertNotIn("[no-match]", text)
        self.assertNotIn("[failed]", text)

    def test_ripgrep_ui_exit_1_no_match_neutral(self) -> None:
        """rg with no match (exit 1) displays [no-match] badge, NOT [failed]."""
        out = io.StringIO()
        ui = TerminalRenderer(stream=out)
        ui.command_completed({
            "command": "rg nonexistent file.txt",
            "exitCode": 1,
            "durationMs": 25,
        })
        text = out.getvalue()
        self.assertIn("[no-match]", text)
        self.assertNotIn("[failed]", text)

    def test_ripgrep_ui_exit_2_syntax_error_failed(self) -> None:
        """rg with invalid arguments (exit 2) displays [failed] exit 2."""
        out = io.StringIO()
        ui = TerminalRenderer(stream=out)
        ui.command_completed({
            "command": "rg --invalid-flag-abc",
            "exitCode": 2,
            "durationMs": 10,
        })
        text = out.getvalue()
        self.assertIn("[failed]", text)
        self.assertIn("exit 2", text)

    def test_non_rg_exit_1_remains_failed(self) -> None:
        """Non-rg commands (jest, eslint, git) with exit 1 still display [failed]."""
        out = io.StringIO()
        ui = TerminalRenderer(stream=out)
        ui.command_completed({
            "command": "npm test",
            "exitCode": 1,
            "durationMs": 500,
        })
        text = out.getvalue()
        self.assertIn("[failed]", text)
        self.assertIn("exit 1", text)
        self.assertNotIn("[no-match]", text)

    def test_rg_raw_exit_preserved_in_evidence(self) -> None:
        """Raw exit code 1 is preserved in command evidence without being rewritten to 0."""
        summary = CommandExecutionSummary(
            command="rg needle file.txt",
            display_command="rg needle file.txt",
            categories=["READ"],
            exit_code=1,
            duration_ms=35,
            sequence=1,
            is_masked=False,
            output_snippet="",
            classification_text="",
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(summary.exit_code, 1)
        outcome = classify_command_outcome(summary)
        # Outcome must NOT be rewritten to PASSED
        self.assertNotEqual(outcome.outcome, "PASSED")
        self.assertEqual(outcome.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
