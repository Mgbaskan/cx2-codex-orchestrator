import unittest
import io
import sys
from pathlib import Path
from unittest.mock import PropertyMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

from terminal_ui import TerminalRenderer
from verification_gate import VerificationAssessment, unwrap_display_command


class TestTerminalUI(unittest.TestCase):

    class TTYStream(io.StringIO):
        def isatty(self):
            return True

    def _make_assessment(self, status="VERIFIED", changed_files=None, valid_cmds=None, reason=""):
        return VerificationAssessment(
            status=status,
            reason=reason,
            evidence_level="STRONG" if status == "VERIFIED" else "NONE",
            requires_continuation=False,
            mutation_detected=bool(changed_files),
            dominant_category="CODE_SOURCE",
            changed_files=changed_files or [],
            valid_evidence_commands=valid_cmds or [],
            executed_commands=valid_cmds or [],
            last_mutation_sequence=0,
            turns_evaluated=1,
        )

    def test_compact_turn_header(self):
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        renderer.render_turn_header(
            session_mode="resume",
            model="gpt-5.6-luna",
            effort="low",
            sandbox="read-only",
            quota={"available": True, "remainingPercent": 27.0, "state": "CONSERVE"}
        )
        out = stream.getvalue()
        self.assertIn("[cx] RESUME · gpt-5.6-luna · low · read-only · 27% kaldı · CONSERVE", out)

    def test_consecutive_turns_reset_diff_state(self):
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        renderer.begin_turn()
        renderer.diff_updated("same diff")
        renderer.begin_turn()
        renderer.diff_updated("same diff")
        self.assertEqual(stream.getvalue().count("same diff"), 2)

    def test_same_item_id_is_not_folded_across_turns(self):
        stream = self.TTYStream()
        renderer = TerminalRenderer(stream=stream, max_command_lines=10)
        folded = "".join(f"line {index}\n" for index in range(12))
        renderer.begin_turn()
        renderer.command_output_delta("same-id", folded)
        renderer.begin_turn()
        renderer.command_output_delta("same-id", "visible next turn\n")
        self.assertIn("visible next turn", stream.getvalue())

    def test_successful_confirmed_response_has_header_and_footer(self):
        stream = self.TTYStream()
        renderer = TerminalRenderer(stream=stream)
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            renderer.begin_turn()
            renderer.agent_delta("line one\nline two")
            renderer.turn_completed("completed", duration_ms=1250, line_count=2)
        output = stream.getvalue()
        self.assertIn("◆ CODEX RESPONSE", output)
        self.assertIn("✓ Completed · 1.2s · 2 lines", output)

    def test_unsuccessful_turns_never_have_success_footer(self):
        for status in ("failed", "interrupted", "idle_timeout", "hard_timeout", "blocked"):
            with self.subTest(status=status):
                stream = self.TTYStream()
                renderer = TerminalRenderer(stream=stream)
                with patch.dict("os.environ", {"NO_COLOR": "1"}):
                    renderer.begin_turn()
                    renderer.agent_delta("partial")
                    renderer.turn_completed(status, duration_ms=100, line_count=1)
                self.assertNotIn("✓ Completed", stream.getvalue())

    def test_non_tty_response_is_plain_newline_complete_without_ansi(self):
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        renderer.begin_turn()
        renderer.agent_delta("answer")
        renderer.turn_completed("completed", duration_ms=10, line_count=1)
        self.assertEqual(stream.getvalue(), "answer\n")
        self.assertNotIn("\x1b[", stream.getvalue())

    def test_tty_markdown_is_streaming_safe_across_delta_boundaries(self):
        stream = self.TTYStream()
        renderer = TerminalRenderer(stream=stream)
        with patch.dict("os.environ", {"NO_COLOR": "1", "TERM": "xterm"}):
            renderer.begin_turn()
            renderer.agent_delta("# Baş")
            renderer.agent_delta("lık\n- **öğe** `kod`\nunfinished **bold")
            renderer.turn_completed("completed")
        output = stream.getvalue()
        self.assertIn("# Başlık\n", output)
        self.assertIn("• öğe kod\n", output)
        self.assertIn("unfinished **bold", output)
        self.assertNotIn("\x1b[", output)

    def test_non_tty_reconciliation_marker_and_authoritative_text_are_visible(self):
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        renderer.begin_turn()
        renderer.agent_delta("stale")
        renderer.response_reconciled("authoritative")
        renderer.turn_completed("completed", line_count=1)
        output = stream.getvalue()
        self.assertIn("stale\n[cx] RESPONSE RECONCILED", output)
        self.assertIn("◆ CODEX RESPONSE · RECONCILED", output)
        self.assertTrue(output.endswith("authoritative\n"))
        self.assertNotIn("\x1b[", output)

    def test_confirmed_empty_response_has_tty_boundary_and_zero_line_footer(self):
        stream = self.TTYStream()
        renderer = TerminalRenderer(stream=stream)
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            renderer.begin_turn()
            renderer.confirm_empty_response()
            renderer.turn_completed("completed", line_count=0)
        output = stream.getvalue()
        self.assertIn("◆ CODEX RESPONSE", output)
        self.assertIn("✓ Completed · 0 lines", output)

    def test_confirmed_empty_response_fabricates_nothing_non_tty(self):
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        renderer.begin_turn()
        renderer.confirm_empty_response()
        renderer.turn_completed("completed", line_count=0)
        self.assertEqual(stream.getvalue(), "")

    def test_semantic_command_unwrap(self):
        raw = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "git status --short"'
        unwrapped = unwrap_display_command(raw)
        self.assertEqual(unwrapped, "git status --short")

    def test_verification_badge_single_line(self):
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        assessment = self._make_assessment(
            status="VERIFIED",
            changed_files=["src/auth.ts"],
            valid_cmds=[{"command": "npm test", "duration_ms": 120, "exit_code": 0}],
        )
        renderer.render_verification_summary(assessment)
        out = stream.getvalue()
        self.assertIn("[doğrulama]", out)
        self.assertIn("VERIFIED", out)
        self.assertIn("1 dosya (src/auth.ts)", out)
        self.assertIn("npm test", out)

    def test_verification_badge_failed_test(self):
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        assessment = self._make_assessment(
            status="FAILED",
            changed_files=["src/app.py"],
            valid_cmds=[{"command": "npm test", "exit_code": 1, "duration_ms": 200}],
            reason="TEST_FAILED_AFTER_CONTINUATION",
        )
        renderer.render_verification_summary(assessment)
        out = stream.getvalue()
        self.assertIn("[doğrulama]", out)
        self.assertIn("FAILED", out)
        self.assertIn("1 dosya (src/app.py)", out)
        self.assertIn("npm test · exit 1", out)

    def test_verification_badge_blocked_env(self):
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        assessment = self._make_assessment(
            status="BLOCKED",
            changed_files=["src/app.py"],
            valid_cmds=[{"command": "go test ./...", "exit_code": 1, "duration_ms": 150}],
            reason="VERIFICATION_BLOCKED_AFTER_CONTINUATION",
        )
        renderer.render_verification_summary(assessment)
        out = stream.getvalue()
        self.assertIn("[doğrulama]", out)
        self.assertIn("BLOCKED", out)
        self.assertIn("1 dosya (src/app.py)", out)

    def test_read_only_audit_badge_complete(self):
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        from verification_gate import AuditEvidenceAssessment
        audit = AuditEvidenceAssessment(
            status="COMPLETE",
            reason="ALL_CHECKS_CONCLUSIVE",
            total_checks=3,
            passed_count=2,
            failed_count=1,
            blocked_count=0,
            inconclusive_count=0,
        )
        assessment = VerificationAssessment(
            status="NOT_APPLICABLE",
            reason="NO_MUTATION",
            evidence_level="NONE",
            requires_continuation=False,
            mutation_detected=False,
            changed_files=[],
            audit_assessment=audit,
        )
        renderer.render_verification_summary(assessment)
        out = stream.getvalue()
        self.assertIn("[audit]", out)
        self.assertIn("COMPLETE", out)
        self.assertIn("3 checks", out)
        self.assertIn("2 passed", out)
        self.assertIn("1 failed", out)

    def test_read_only_audit_badge_partial(self):
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        from verification_gate import AuditEvidenceAssessment
        audit = AuditEvidenceAssessment(
            status="PARTIAL",
            reason="SOME_CHECKS_BLOCKED",
            total_checks=2,
            passed_count=1,
            failed_count=0,
            blocked_count=1,
            inconclusive_count=0,
        )
        assessment = VerificationAssessment(
            status="NOT_APPLICABLE",
            reason="NO_MUTATION",
            evidence_level="NONE",
            requires_continuation=False,
            mutation_detected=False,
            changed_files=[],
            audit_assessment=audit,
        )
        renderer.render_verification_summary(assessment)
        out = stream.getvalue()
        self.assertIn("[audit]", out)
        self.assertIn("PARTIAL", out)
        self.assertIn("2 checks", out)
        self.assertIn("1 passed", out)
        self.assertIn("1 blocked", out)


if __name__ == "__main__":
    unittest.main()
