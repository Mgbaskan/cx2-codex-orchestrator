from __future__ import annotations

import re


def _render_line(raw: str, *, color: bool, in_code: bool) -> tuple[str, bool]:
    line = raw
    if line.strip().startswith("```"):
        return "  " + ("─" * 3), not in_code
    if in_code:
        return line, in_code
    heading = re.match(r"^(#{1,6})\s+(.*)$", line)
    if heading:
        title = heading.group(2).strip()
        return (("◆ " + title) if color else ("# " + title)), in_code
    bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
    if bullet:
        line = "• " + bullet.group(1)
    elif re.match(r"^\s*>\s?", line):
        line = "│ " + re.sub(r"^\s*>\s?", "", line)
    elif re.match(r"^\s*(\*{3,}|-{3,}|_{3,})\s*$", line):
        line = "─" * 24
    line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
    line = re.sub(r"`([^`]+)`", r"\1", line)
    line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", line)
    return line, in_code


class TerminalMarkdownStream:
    """Line-buffered Markdown presentation that is safe across delta splits."""

    def __init__(self) -> None:
        self._buffer = ""
        self._in_code = False

    def feed(self, value: str, *, color: bool = False) -> str:
        self._buffer += str(value)
        rendered: list[str] = []
        while "\n" in self._buffer:
            raw, self._buffer = self._buffer.split("\n", 1)
            raw = raw[:-1] if raw.endswith("\r") else raw
            line, self._in_code = _render_line(raw, color=color, in_code=self._in_code)
            rendered.append(line + "\n")
        return "".join(rendered)

    def finish(self, *, color: bool = False) -> str:
        if not self._buffer:
            return ""
        raw = self._buffer
        self._buffer = ""
        line, self._in_code = _render_line(raw, color=color, in_code=self._in_code)
        return line


def render_markdown(text: str, *, color: bool = False) -> str:
    """Presentation-only Markdown reduction; source text is never modified."""
    stream = TerminalMarkdownStream()
    return stream.feed(str(text), color=color) + stream.finish(color=color)


__all__ = ["TerminalMarkdownStream", "render_markdown"]
