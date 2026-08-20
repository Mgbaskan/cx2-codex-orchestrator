import unittest
import io
import sys
from pathlib import Path
from unittest.mock import PropertyMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "runtime" / "cx2"))

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

    def test_approval_prompt_safe_default(self):
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        with patch.object(TerminalRenderer, "can_prompt", new_callable=PropertyMock, return_value=True), patch("builtins.input", return_value=""):
            decision = renderer.approval_prompt(
                title="Run command",
                details=["npm test"],
                decisions=["accept", "decline"],
                default_decision="decline",
            )
            self.assertEqual(decision, "decline")


if __name__ == "__main__":
    unittest.main()
