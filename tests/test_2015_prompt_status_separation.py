from __future__ import annotations

import io
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap  # noqa: E402
sys.path.insert(0, str(_bootstrap.RUNTIME_DIR))
sys.path.insert(0, str(_bootstrap.SRC_DIR))

from terminal_pager import cell_width, TerminalPager  # noqa: E402
from terminal_ui import TerminalRenderer, _truncate_to_cell_width  # noqa: E402
import cx2_cli  # noqa: E402


class TTYStream(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\r")


def normalize_terminal_output(raw: str) -> str:
    """Normalize ANSI escape sequences to inspect semantic visual line structure."""
    return _ANSI_RE.sub("", raw)


def make_mock_input(stream: io.StringIO, return_value: str = "") -> Callable[[str], str]:
    """Simulates standard interactive input() which writes prompt to stream then returns text."""
    def _mock_input(prompt: str) -> str:
        stream.write(prompt)
        return return_value
    return _mock_input


class TestPromptStatusSeparation2015(unittest.TestCase):
    def setUp(self) -> None:
        self.default_quota = {
            "available": True,
            "remainingPercent": 23.0,
            "state": "CONSERVE",
            "capturedAt": "2026-08-29T12:00:00Z",
        }
        self.default_context = {"percent": 61.0}

    def _create_renderer(self, stream: Any | None = None) -> TerminalRenderer:
        s = stream or TTYStream()
        renderer = TerminalRenderer(stream=s)
        renderer.set_status_snapshot(
            quota=self.default_quota,
            context=self.default_context,
            model="gpt-5.6-luna",
            effort="LOW",
            sandbox="READ-ONLY",
        )
        return renderer

    def test_a_initial_idle_interactive_prompt_has_single_blank_row_separation(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            user_input = renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "explain auth"))
            self.assertEqual(user_input, "explain auth")

        raw = stream.getvalue()
        # Verify raw row advance exists
        self.assertIn("\r\n\r\n\r", raw)
        self.assertIn("CX> ", raw)

        # Check visual line structure
        norm = normalize_terminal_output(raw)
        lines = [line.strip() for line in norm.replace("\r\n", "\n").split("\n")]
        non_empty = [l for l in lines if l]
        self.assertTrue(any("gpt-5.6-luna" in l and "context 61%" in l for l in non_empty))
        self.assertTrue(any(l.startswith("CX>") for l in non_empty))

        # Assert no row collapse
        self.assertNotIn("context 61%CX>", raw)
        self.assertNotIn("context 61% CX>", raw)

    def test_b_after_ordinary_completed_turn(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            renderer.begin_turn()
            renderer.render_turn_header(
                session_mode="resume",
                model="gpt-5.6-luna",
                effort="low",
                sandbox="read-only",
                quota=self.default_quota,
            )
            renderer.agent_delta("Turn response text\n")
            renderer.turn_completed("completed", duration_ms=1200, line_count=1)
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "next turn"))

        raw = stream.getvalue()
        norm = normalize_terminal_output(raw)
        self.assertIn("✓ Completed · 1.2s · 1 lines", norm)
        self.assertIn("\r\n\r\n\r", raw)
        self.assertIn("CX> ", raw)
        self.assertNotIn("context 61%CX>", raw)
        self.assertNotIn("context 61% CX>", raw)

    def test_c_after_tool_command_activity(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            renderer.begin_turn()
            renderer.command_started("git status")
            renderer.command_completed({"command": "git status", "status": "completed", "exitCode": 0, "durationMs": 15})
            renderer.turn_completed("completed", duration_ms=200, line_count=0)
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "status checked"))

        raw = stream.getvalue()
        self.assertIn("\r\n\r\n\r", raw)
        self.assertIn("CX> ", raw)
        self.assertNotIn("context 61%CX>", raw)

    def test_d_after_codex_response_and_completion_footer(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            renderer.begin_turn()
            renderer.agent_delta("Here is the answer.")
            renderer.turn_completed("completed", duration_ms=900, line_count=1)
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "ok"))

        raw = stream.getvalue()
        norm = normalize_terminal_output(raw)
        self.assertIn("◆ CODEX RESPONSE", norm)
        self.assertIn("✓ Completed · 0.9s · 1 lines", norm)
        self.assertIn("\r\n\r\n\r", raw)
        self.assertIn("CX> ", raw)

    def test_e_after_last_transcript_command(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            # Simulate /last printing output
            renderer.suspend_status()
            stream.write("=== LAST TRANSCRIPT ===\nLine 1\nLine 2\n")
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "/help"))

        raw = stream.getvalue()
        self.assertIn("=== LAST TRANSCRIPT ===", raw)
        self.assertIn("\r\n\r\n\r", raw)
        self.assertIn("CX> ", raw)

    def test_f_after_pager_exit(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            prev = renderer.suspend_presentation("pager")
            # Pager displays and exits
            pager = TerminalPager(stream=stream, input_stream=TTYStream("q\n"))
            pager.show("Paged response line 1\nPaged response line 2\n")
            renderer.restore_presentation(prev)
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "after pager"))

        raw = stream.getvalue()
        self.assertIn("Paged response line 1", raw)
        self.assertIn("\r\n\r\n\r", raw)
        self.assertIn("CX> ", raw)

    def test_g_after_trace_command(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            renderer.suspend_status()
            stream.write("[cx] 1. git status · completed · exit=0 · 10ms\n")
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "after trace"))

        raw = stream.getvalue()
        self.assertIn("[cx] 1. git status", raw)
        self.assertIn("\r\n\r\n\r", raw)
        self.assertIn("CX> ", raw)

    def test_h_after_quota_command(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            renderer.suspend_status()
            stream.write("=== CX QUOTA ===\nQuota: 23%\n")
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "after quota"))

        raw = stream.getvalue()
        self.assertIn("=== CX QUOTA ===", raw)
        self.assertIn("\r\n\r\n\r", raw)
        self.assertIn("CX> ", raw)

    def test_i_after_paste_cancel(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            prev = renderer.suspend_presentation("paste")
            # Multiline paste cancel
            stream.write("[cx] Çok satırlı giriş iptal edildi.\n")
            renderer.restore_presentation(prev)
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "after paste"))

        raw = stream.getvalue()
        self.assertIn("[cx] Çok satırlı giriş iptal edildi.", raw)
        self.assertIn("\r\n\r\n\r", raw)
        self.assertIn("CX> ", raw)

    def test_j_after_approval_suspend_and_resume(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)), \
                 patch("builtins.input", return_value="1"):
                decision = renderer.approval_prompt(
                    title="Execute git",
                    details=["git status"],
                    decisions=["accept", "decline"],
                    default_decision="accept",
                )
            self.assertEqual(decision, "accept")
            renderer.turn_completed("completed", duration_ms=100, line_count=1)
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "after approval"))

        raw = stream.getvalue()
        norm = normalize_terminal_output(raw)
        self.assertIn("[onay] Execute git", norm)
        self.assertIn("\r\n\r\n\r", raw)
        self.assertIn("CX> ", raw)

    def test_k_after_interruption(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            renderer.begin_turn()
            renderer.interrupting()
            renderer.turn_completed("interrupted", duration_ms=50, line_count=0)
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "after ctrl+c"))

        raw = stream.getvalue()
        norm = normalize_terminal_output(raw)
        self.assertIn("İşlem kesiliyor...", norm)
        self.assertIn("\r\n\r\n\r", raw)
        self.assertIn("CX> ", raw)

    def test_l_repeated_status_redraws_never_accumulate_blank_lines(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            # Render status line multiple times before prompting
            for _ in range(5):
                renderer.render_status_line()
            # Then call prompt_input
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "turn 1"))

        raw = stream.getvalue()
        # Exactly one \r\n\r\n\r row advance sequence exists
        self.assertEqual(raw.count("\r\n\r\n\r"), 1)
        # Verify visual structure: exactly 1 blank line between status and prompt
        norm = normalize_terminal_output(raw)
        lines = [l.strip() for l in norm.replace("\r\n", "\n").split("\n")]
        # Remove empty lines at the very end
        while lines and not lines[-1]:
            lines.pop()
        # There should be status line, 1 empty line, and prompt line
        self.assertEqual(lines.count(""), 1)

    def test_m_prompt_owner_status_deferral(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)

        def mock_input(prompt: str) -> str:
            stream.write(prompt)
            # During prompt ownership, trigger a status update
            before_len = len(stream.getvalue())
            renderer.set_status_snapshot(
                quota={"available": True, "remainingPercent": 99.0, "state": "NORMAL"},
                context={"percent": 10.0},
            )
            renderer.render_status_line()
            after_len = len(stream.getvalue())
            # Assert zero terminal I/O while prompt owns terminal
            self.assertEqual(before_len, after_len)
            self.assertTrue(renderer._status_dirty)
            return "typed command"

        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            result = renderer.prompt_input("CX> ", input_func=mock_input)
            self.assertEqual(result, "typed command")

    def test_n_post_prompt_line_safety(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "committed user command"))
            stream.write("committed user command\n")

            # Now begin turn; verify suspend_status or start_activity does not erase prompt line
            renderer.begin_turn()
            renderer.suspend_status()
            # suspend_status must not have emitted \r\x1b[2K on the prompt line
            self.assertFalse(renderer._status_cursor_active)
            renderer.agent_delta("New answer\n")

        raw = stream.getvalue()
        self.assertIn("committed user command\n", raw)
        self.assertIn("New answer\n", raw)

    def test_o_raw_sequence_column_zero(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "test"))

        raw = stream.getvalue()
        # Must contain exact row advance \r\n\r\n\r
        self.assertIn("\r\n\r\n\r", raw)
        idx = raw.index("\r\n\r\n\r")
        after_advance = raw[idx + len("\r\n\r\n\r"):]
        self.assertTrue(after_advance.startswith("CX> "))

    def test_p_narrow_and_tiny_terminal_bounding(self) -> None:
        full_text = "[cx] gpt-5.6-luna · LOW · READ-ONLY · quota 23% · CONSERVE · 0m old · context 61%"
        test_widths = [1, 2, 3, 4, 8, 10, 40, 50, 100]

        for width in test_widths:
            with self.subTest(width=width):
                avail = max(1, width - 1)
                truncated = _truncate_to_cell_width(full_text, avail)
                rendered_width = cell_width(truncated)
                self.assertLessEqual(rendered_width, avail)
                self.assertGreaterEqual(rendered_width, 0)
                if avail <= 3:
                    self.assertNotIn("...", truncated)
                elif cell_width(full_text) > avail:
                    self.assertTrue(truncated.endswith("..."))

    def test_q_environment_permutations(self) -> None:
        envs = [
            {"TERM": "xterm", "NO_COLOR": ""},
            {"TERM": "xterm", "NO_COLOR": "1"},
            {"TERM": "", "TERM_PROGRAM": "vscode", "NO_COLOR": ""},
        ]
        for env in envs:
            with self.subTest(env=env):
                stream = TTYStream()
                renderer = self._create_renderer(stream)
                with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, env, clear=True):
                    renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "env check"))
                raw = stream.getvalue()
                self.assertIn("\r\n\r\n\r", raw)
                self.assertNotIn("context 61%CX>", raw)

    def test_r_non_tty_preserves_plain_deterministic_output(self) -> None:
        stream = io.StringIO()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", io.StringIO()):
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "scripted input"))

        raw = stream.getvalue()
        # Non-TTY must not emit ANSI cursor sequences or decorative \r\n\r\n\r
        self.assertNotIn("\x1b[", raw)
        self.assertNotIn("\r\n\r\n\r", raw)

    def test_visual_semantic_layout_and_forbidden_concatenation(self) -> None:
        stream = TTYStream()
        renderer = self._create_renderer(stream)
        with patch.object(sys, "stdin", TTYStream()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            renderer.prompt_input("CX> ", input_func=make_mock_input(stream, "sample query"))

        raw = stream.getvalue()
        # Assert impossible bad UX
        self.assertNotIn("context 61%CX>", raw)
        self.assertNotIn("context 61% CX>", raw)

        # Inspect normalized visual rows
        raw_parts = raw.split("\r\n\r\n\r")
        self.assertEqual(len(raw_parts), 2)
        status_part = normalize_terminal_output(raw_parts[0]).strip()
        self.assertIn("[cx]", status_part)
        self.assertIn("context 61%", status_part)
        prompt_part = normalize_terminal_output(raw_parts[1]).strip()
        self.assertTrue(prompt_part.startswith("CX>"))


if __name__ == "__main__":
    unittest.main()
