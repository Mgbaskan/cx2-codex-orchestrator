from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from array import array
from typing import Any

from terminal_safety import sanitize_untrusted_text


def cell_width(value: str) -> int:
    total = 0
    for char in value:
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return total


class LazyWrappedLines:
    """Near-linear, page-lazy wrapping with compact 32-bit source spans."""

    def __init__(self, text: str, width: int) -> None:
        self.text = str(text)
        self.width = max(1, int(width))
        self._spans = array("I")
        self._scan_index = 0
        self._done = False
        self._empty_emitted = False

    @property
    def complete(self) -> bool:
        return self._done

    @property
    def known_lines(self) -> int:
        return len(self._spans) // 2

    def _scan_next(self) -> None:
        if self._done:
            return
        length = len(self.text)
        start = self._scan_index
        if start >= length:
            if length == 0 and not self._empty_emitted:
                self._spans.extend((0, 0))
                self._empty_emitted = True
            self._done = True
            return
        index = start
        row_width = 0
        while index < length:
            char = self.text[index]
            if char == "\n":
                self._spans.extend((start, index))
                self._scan_index = index + 1
                return
            char_width = cell_width(char)
            if index > start and row_width + char_width > self.width:
                self._spans.extend((start, index))
                self._scan_index = index
                return
            row_width += char_width
            index += 1
        self._spans.extend((start, length))
        self._scan_index = length
        self._done = True

    def ensure(self, line_count: int) -> None:
        while self.known_lines < line_count and not self._done:
            self._scan_next()

    def page(self, offset: int, height: int) -> list[str]:
        self.ensure(max(0, offset) + max(0, height))
        end = min(self.known_lines, offset + height)
        return [
            self.text[self._spans[index * 2] : self._spans[index * 2 + 1]]
            for index in range(max(0, offset), end)
        ]

    def line_count(self) -> int:
        while not self._done:
            self._scan_next()
        return self.known_lines


def wrap_display(text: str, width: int) -> list[str]:
    wrapped = LazyWrappedLines(str(text), width)
    return wrapped.page(0, wrapped.line_count())


def pager_capable(stream: Any | None = None, input_stream: Any | None = None) -> bool:
    stream = sys.stdout if stream is None else stream
    input_stream = sys.stdin if input_stream is None else input_stream
    if os.environ.get("CX2_STATIC_UI", "").casefold() in {"1", "true", "yes", "on"}:
        return False
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
        presentation_text = sanitize_untrusted_text(canonical_text)
        if not pager_capable(self.stream, self.input_stream):
            self.stream.write(presentation_text)
            if presentation_text and not presentation_text.endswith("\n"):
                self.stream.write("\n")
            self.stream.flush()
            return
        width = max(20, shutil.get_terminal_size((100, 24)).columns)
        lines = LazyWrappedLines(presentation_text, width)
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
                    lines = LazyWrappedLines(presentation_text, width)
                    lines.ensure(offset + 1)
                    offset = min(offset, max(0, lines.known_lines - 1))
                lines.ensure(offset + 1)
                if offset >= lines.known_lines and lines.complete:
                    break
                clear_rendered()
                page = lines.page(offset, height)
                self.stream.write("\r\x1b[2K" + "\n".join(page))
                rendered_rows = len(page)
                at_end = lines.complete and offset + len(page) >= lines.known_lines
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
                    lines.ensure(offset + 2)
                    offset = min(max(0, lines.known_lines - 1), offset + 1)
                elif key in {"pgup", "b"}:
                    offset = max(0, offset - height)
                elif key == "home":
                    offset = 0
                elif key == "end":
                    offset = max(0, lines.line_count() - height)
                elif key in {" ", "pgdn"}:
                    lines.ensure(offset + height + 1)
                    offset = min(max(0, lines.known_lines - 1), offset + height)
                elif key in {"\n", "\r"}:
                    lines.ensure(offset + 2)
                    offset = min(max(0, lines.known_lines - 1), offset + 1)
        except KeyboardInterrupt:
            return
        except Exception:
            # Pager failure is presentation-only; fall back to the canonical
            # complete text rather than risking response loss.
            clear_rendered()
            self.stream.write(presentation_text)
            if presentation_text and not presentation_text.endswith("\n"):
                self.stream.write("\n")
        finally:
            # Clear prompt and leave cursor on a clean line even after Ctrl+C.
            clear_rendered()
            self.stream.flush()


def page_text(text: str, *, stream: Any | None = None, input_stream: Any | None = None) -> None:
    TerminalPager(stream=stream, input_stream=input_stream).show(text)


__all__ = [
    "LazyWrappedLines", "TerminalPager", "cell_width", "page_text",
    "pager_capable", "wrap_display",
]
