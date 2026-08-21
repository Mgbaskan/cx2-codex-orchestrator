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
