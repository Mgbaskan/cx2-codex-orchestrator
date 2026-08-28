from __future__ import annotations

"""Presentation-only safety for text controlled outside CX2."""


def sanitize_untrusted_text(value: object) -> str:
    """Make terminal controls visible and inert without changing canonical text."""

    rendered: list[str] = []
    for char in str(value):
        codepoint = ord(char)
        if char in {"\n", "\t"}:
            rendered.append(char)
        elif char == "\r":
            rendered.append("\\r")
        elif codepoint < 0x20 or codepoint == 0x7F:
            rendered.append(f"\\x{codepoint:02x}")
        elif 0x80 <= codepoint <= 0x9F:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(char)
    return "".join(rendered)


__all__ = ["sanitize_untrusted_text"]
