from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from typing import Any


def cell_width(value: str) -> int:
    total = 0
    for char in value:
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return total


def wrap_display(text: str, width: int) -> list[str]:
    width = max(1, int(width))
    lines: list[str] = []
    for source in str(text).splitlines() or [""]:
        if not source:
            lines.append("")
            continue
        current = ""
        for char in source:
            if current and cell_width(current + char) > width:
                lines.append(current)
                current = ""
            current += char
        lines.append(current)
    return lines


def pager_capable(stream: Any | None = None, input_stream: Any | None = None) -> bool:
    stream = sys.stdout if stream is None else stream
    input_stream = sys.stdin if input_stream is None else input_stream
    if os.environ.get("TERM", "").casefold() == "dumb":
        return False
    if os.environ.get("NO_COLOR") is not None and os.name != "nt":
        # NO_COLOR does not prohibit paging, but redirected streams do.
        pass
    try:
        return bool(getattr(stream, "isatty", lambda: False)()) and bool(
            getattr(input_stream, "isatty", lambda: False)()
        )
    except Exception:
        return False


class TerminalPager:
    """Small, dependency-free pager that always restores terminal state."""

    def __init__(self, *, stream: Any | None = None, input_stream: Any | None = None) -> None:
        self.stream = sys.stdout if stream is None else stream
        self.input_stream = sys.stdin if input_stream is None else input_stream

    def _key(self) -> str:
        if os.name == "nt":
            import msvcrt
            key = msvcrt.getwch()
            if key in {"\x00", "\xe0"}:
                return {
                    "H": "up", "P": "down", "I": "pgup", "Q": "pgdn",
                    "G": "home", "O": "end",
                }.get(msvcrt.getwch(), "special")
            return key
        import termios
        import tty
        import select
        fd = self.input_stream.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            first = os.read(fd, 1).decode("utf-8", errors="replace")
            if first != "\x1b":
                return first
            if not select.select([fd], [], [], 0.03)[0]:
                return "escape"
            tail_bytes = bytearray()
            for _ in range(5):
                if not select.select([fd], [], [], 0.01)[0]:
                    break
                tail_bytes.extend(os.read(fd, 1))
            tail = bytes(tail_bytes).decode("utf-8", errors="replace")
            return {
                "[A": "up", "[B": "down", "[5~": "pgup", "[6~": "pgdn",
                "[H": "home", "[F": "end", "OH": "home", "OF": "end",
            }.get(tail, "escape")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def show(self, text: str) -> None:
        canonical_text = str(text)
        if not pager_capable(self.stream, self.input_stream):
            self.stream.write(canonical_text)
            if canonical_text and not canonical_text.endswith("\n"):
                self.stream.write("\n")
            self.stream.flush()
            return
        width = max(20, shutil.get_terminal_size((100, 24)).columns)
        lines = wrap_display(canonical_text, width)
        offset = 0
        rendered_rows = 0
        navigated = False

        def clear_rendered() -> None:
            nonlocal rendered_rows
            if rendered_rows <= 0:
                return
            self.stream.write("\r\x1b[2K")
            for _ in range(rendered_rows - 1):
                self.stream.write("\x1b[1A\r\x1b[2K")
            rendered_rows = 0

        try:
            while True:
                new_width = max(20, shutil.get_terminal_size((100, 24)).columns)
                height = max(4, shutil.get_terminal_size((100, 24)).lines - 2)
                if new_width != width:
                    width = new_width
                    lines = wrap_display(canonical_text, width)
                    offset = min(offset, max(0, len(lines) - 1))
                if offset >= len(lines):
                    break
                clear_rendered()
                page = lines[offset : offset + height]
                self.stream.write("\r\x1b[2K" + "\n".join(page))
                rendered_rows = len(page)
                at_end = offset + len(page) >= len(lines)
                if at_end and not navigated:
                    self.stream.write("\n")
                    rendered_rows = 0
                    break
                prompt = "-- end --" if at_end else "-- more --"
                self.stream.write(
                    f"\n\x1b[2K{prompt} (Space/Enter next, b back, q quit)\r"
                )
                rendered_rows += 1
                self.stream.flush()
                key = self._key().casefold()
                if key in {"q", "\x03", "\x1b", "escape"}:
                    break
                navigated = True
                if key == "up":
                    offset = max(0, offset - 1)
                elif key == "down":
                    offset = min(len(lines) - 1, offset + 1)
                elif key in {"pgup", "b"}:
                    offset = max(0, offset - height)
                elif key == "home":
                    offset = 0
                elif key == "end":
                    offset = max(0, len(lines) - height)
                elif key in {" ", "pgdn"}:
                    offset = min(max(0, len(lines) - 1), offset + height)
                elif key in {"\n", "\r"}:
                    offset = min(max(0, len(lines) - 1), offset + 1)
        except KeyboardInterrupt:
            return
        except Exception:
            # Pager failure is presentation-only; fall back to the canonical
            # complete text rather than risking response loss.
            clear_rendered()
            self.stream.write(canonical_text)
            if canonical_text and not canonical_text.endswith("\n"):
                self.stream.write("\n")
        finally:
            # Clear prompt and leave cursor on a clean line even after Ctrl+C.
            clear_rendered()
            self.stream.flush()


def page_text(text: str, *, stream: Any | None = None, input_stream: Any | None = None) -> None:
    TerminalPager(stream=stream, input_stream=input_stream).show(text)


__all__ = ["TerminalPager", "cell_width", "page_text", "pager_capable", "wrap_display"]
