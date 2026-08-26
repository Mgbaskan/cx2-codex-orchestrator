from __future__ import annotations

import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "cx2"))

from terminal_pager import TerminalPager, cell_width, page_text, wrap_display  # noqa: E402
from terminal_markdown import render_markdown  # noqa: E402


class TestTerminalPagerAndMarkdown(unittest.TestCase):
    def test_wrap_is_cell_width_aware_and_plain_fallback_is_complete(self) -> None:
        self.assertGreater(cell_width("界"), cell_width("a"))
        self.assertEqual(wrap_display("abcdef", 3), ["abc", "def"])
        out = io.StringIO()
        page_text("line 1\nline 2", stream=out, input_stream=io.StringIO())
        self.assertEqual(out.getvalue(), "line 1\nline 2\n")
        self.assertNotIn("\x1b", out.getvalue())

    def test_markdown_is_presentation_only(self) -> None:
        source = "# Heading\n\n- **bold** `code`\n> quote\n\n```\nprint(1)\n```"
        rendered = render_markdown(source)
        self.assertIn("# Heading", rendered)
        self.assertIn("• bold code", rendered)
        self.assertIn("│ quote", rendered)
        self.assertIn("print(1)", rendered)
        self.assertEqual(source.splitlines()[0], "# Heading")

    def test_status_snapshot_reports_age_and_unavailable_without_polling(self) -> None:
        from datetime import datetime, timezone, timedelta
        from terminal_ui import TerminalRenderer
        renderer = TerminalRenderer(stream=io.StringIO())
        captured = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        status = renderer.set_status_snapshot(
            quota={"remainingPercent": 22, "capturedAt": captured},
            context={"percent": 44},
            model="SOL",
            effort="high",
            sandbox="write",
        )
        self.assertIn("SOL", status)
        self.assertIn("quota 22%", status)
        self.assertIn("old", status)
        self.assertIn("context 44%", status)
        unavailable = renderer.set_status_snapshot(quota={"available": False})
        self.assertIn("quota 22%", unavailable)
        self.assertIn("refresh unavailable", unavailable)

    def test_tty_navigation_does_not_skip_space_or_enter(self) -> None:
        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        class KeysPager(TerminalPager):
            def __init__(self, keys: list[str], **kwargs) -> None:
                super().__init__(**kwargs)
                self.keys = iter(keys)

            def _key(self) -> str:
                return next(self.keys)

        text = "\n".join(f"line-{index}" for index in range(10))
        for keys, expected in [([" ", "q"], "line-4"), (["\r", "q"], "line-1")]:
            out = TTY()
            with patch.dict("os.environ", {"TERM": "xterm"}), patch("terminal_pager.shutil.get_terminal_size", return_value=type("S", (), {"columns": 80, "lines": 6})()):
                KeysPager(keys, stream=out, input_stream=TTY()).show(text)
            self.assertIn(expected, out.getvalue())
            self.assertIn("\x1b[2K", out.getvalue())

    def test_tty_render_failure_restores_and_emits_complete_fallback(self) -> None:
        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        class FailingPager(TerminalPager):
            def _key(self) -> str:
                raise RuntimeError("synthetic raw-mode failure")

        text = "alpha\nbeta\ngamma\ndelta\nepsilon"
        out = TTY()
        with patch.dict("os.environ", {"TERM": "xterm"}), patch("terminal_pager.shutil.get_terminal_size", return_value=type("S", (), {"columns": 80, "lines": 6})()):
            FailingPager(stream=out, input_stream=TTY()).show(text)
        self.assertIn(text, out.getvalue())
        self.assertIn("\x1b[1A\r\x1b[2K", out.getvalue())
        self.assertTrue(out.getvalue().endswith("\n"))

    def test_all_navigation_controls_quit_keys_resize_empty_and_huge(self) -> None:
        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        class KeysPager(TerminalPager):
            def __init__(self, keys: list[str], **kwargs) -> None:
                super().__init__(**kwargs)
                self.keys = iter(keys)

            def _key(self) -> str:
                return next(self.keys)

        size = type("S", (), {"columns": 80, "lines": 6})()
        text = "\n".join(f"line-{index}" for index in range(10))
        cases = (
            (["down", "q"], "line-1", 2),
            (["up", "q"], "line-0", 2),
            (["pgdn", "pgup", "q"], "line-0", 2),
            (["pgdn", "b", "q"], "line-0", 2),
            (["end", "q"], "line-9", 1),
            (["end", "home", "q"], "line-0", 2),
        )
        for keys, marker, minimum in cases:
            with self.subTest(keys=keys):
                out = TTY()
                with patch.dict("os.environ", {"TERM": "xterm"}), patch(
                    "terminal_pager.shutil.get_terminal_size", return_value=size
                ):
                    KeysPager(keys, stream=out, input_stream=TTY()).show(text)
                self.assertGreaterEqual(out.getvalue().count(marker), minimum)

        for quit_key in ("q", "escape", "\x03"):
            out = TTY()
            with patch.dict("os.environ", {"TERM": "xterm"}), patch(
                "terminal_pager.shutil.get_terminal_size", return_value=size
            ):
                KeysPager([quit_key], stream=out, input_stream=TTY()).show(text)
            self.assertNotIn("line-4", out.getvalue())

        out = TTY()
        sizes = [
            type("S", (), {"columns": 80, "lines": 6})(),
            type("S", (), {"columns": 40, "lines": 6})(),
            type("S", (), {"columns": 40, "lines": 6})(),
        ]
        with patch.dict("os.environ", {"TERM": "xterm"}), patch(
            "terminal_pager.shutil.get_terminal_size", side_effect=sizes
        ):
            KeysPager(["q"], stream=out, input_stream=TTY()).show("x" * 200)
        self.assertIn("-- more --", out.getvalue())
        self.assertIn("\x1b[2K", out.getvalue())

        for payload in ("", "\n".join("x" for _ in range(10000))):
            out = TTY()
            keys = [] if payload == "" else ["q"]
            with patch.dict("os.environ", {"TERM": "xterm"}), patch(
                "terminal_pager.shutil.get_terminal_size", return_value=size
            ):
                KeysPager(keys, stream=out, input_stream=TTY()).show(payload)
            self.assertTrue(out.getvalue() or payload == "")

    def test_markdown_contract_and_plain_mode(self) -> None:
        source = (
            "# H1\n## H2\n**kalın** ve `kod`\n- öğe\n> alıntı\n"
            "[bağlantı](https://example.test)\n---\n| a | b |\n**unfinished 🚀"
        )
        plain = render_markdown(source, color=False)
        self.assertIn("# H1", plain)
        self.assertIn("# H2", plain)
        self.assertIn("kalın", plain)
        self.assertIn("kod", plain)
        self.assertIn("• öğe", plain)
        self.assertIn("│ alıntı", plain)
        self.assertIn("bağlantı (https://example.test)", plain)
        self.assertIn("| a | b |", plain)
        self.assertNotIn("\x1b", plain)

    def test_terminal_capabilities_degrade_independently(self) -> None:
        from terminal_ui import TerminalRenderer

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        class ASCII(TTY):
            @property
            def encoding(self) -> str:
                return "ascii"

        with patch.object(sys, "stdin", TTY()), patch.dict(
            "os.environ", {"TERM": "xterm", "NO_COLOR": "1"}
        ):
            renderer = TerminalRenderer(stream=ASCII())
            caps = renderer.capabilities
            self.assertTrue(caps.tty)
            self.assertTrue(caps.cursor)
            self.assertTrue(caps.sticky_status)
            self.assertFalse(caps.color)
            self.assertFalse(caps.unicode)

        with patch.object(sys, "stdin", TTY()), patch.dict(
            "os.environ", {"TERM": "dumb"}
        ):
            caps = TerminalRenderer(stream=TTY()).capabilities
            self.assertFalse(caps.cursor)
            self.assertFalse(caps.sticky_status)

        caps = TerminalRenderer(stream=io.StringIO()).capabilities
        self.assertFalse(caps.tty)
        self.assertFalse(caps.cursor)

    def test_status_suspends_for_stream_and_resumes_without_lost_bytes(self) -> None:
        from terminal_ui import TerminalRenderer

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        stream = TTY()
        with patch.object(sys, "stdin", TTY()), patch.dict(
            "os.environ", {"TERM": "xterm", "NO_COLOR": "1"}
        ):
            renderer = TerminalRenderer(stream=stream)
            renderer.set_status_snapshot(
                quota={"available": True, "remainingPercent": 50},
                model="model", effort="low", sandbox="read-only",
            )
            renderer.render_status_line()
            renderer.agent_delta("exact streamed bytes")
            renderer.turn_completed("completed")
        output = stream.getvalue()
        self.assertIn("exact streamed bytes", output)
        self.assertGreaterEqual(output.count("quota 50%"), 2)
        self.assertNotIn("\x1b[3", output)


if __name__ == "__main__":
    unittest.main()
