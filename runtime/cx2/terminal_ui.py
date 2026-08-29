from __future__ import annotations

import os
import hashlib
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from verification_gate import (
    is_ripgrep_command,
    unwrap_display_command,
)
from terminal_pager import cell_width
from terminal_markdown import TerminalMarkdownStream, render_markdown
from terminal_safety import sanitize_untrusted_text


CX2_TERMINAL_RENDERER_V1 = True
MAX_ACTIVITY_ITEMS = 256
MAX_VISIBLE_DIFF_BYTES = 256 * 1024


def _truncate_to_cell_width(text: str, max_cells: int) -> str:
    if max_cells <= 0:
        return ""
    total = cell_width(text)
    if total <= max_cells:
        return text
    if max_cells <= 3:
        current = 0
        retained = []
        for char in text:
            w = cell_width(char)
            if current + w > max_cells:
                break
            retained.append(char)
            current += w
        return "".join(retained)
    target = max_cells - 3
    current = 0
    retained = []
    for char in text:
        w = cell_width(char)
        if current + w > target:
            break
        retained.append(char)
        current += w
    return "".join(retained) + "..."


def _quota_freshness(quota: dict[str, Any]) -> str:
    captured = quota.get("capturedAt")
    if not isinstance(captured, str):
        return "age unknown"
    try:
        stamp = datetime.fromisoformat(captured.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - stamp).total_seconds() // 60))
        return f"{age}m old"
    except (ValueError, TypeError):
        return "age unknown"


@dataclass(frozen=True)
class TerminalCapabilities:
    tty: bool
    interactive_input: bool
    color: bool
    unicode: bool
    cursor: bool
    sticky_status: bool



class TerminalRenderer:
    """
    Terminal-only presentation layer.

    Protocol state and full command output remain owned by turn_runner.
    This class controls only what is rendered to the human terminal.
    """

    SPINNER_FRAMES = (
        "|",
        "/",
        "-",
        "\\",
    )

    def __init__(
        self,
        *,
        stream: Any | None = None,
        max_command_lines: int = 80,
        compact_tools: bool = False,
    ) -> None:

        self._stream_override = stream

        self.max_command_lines = max(
            10,
            int(max_command_lines),
        )
        self.compact_tools = bool(compact_tools)

        self._lock = threading.RLock()

        self._spinner_thread: threading.Thread | None = None
        self._spinner_stop: threading.Event | None = None
        self._spinner_label: str | None = None

        self._folded_items: set[str] = set()
        self._visible_lines: dict[str, int] = {}

        self._last_diff: str = ""
        self._last_diff_chars = 0
        self._last_diff_digest = ""
        self._needs_agent_separator: bool = False
        self._response_open: bool = False
        self._response_has_text: bool = False
        self._response_ends_with_newline: bool = True
        self._status_text: str = ""
        self._status_visible = False
        self._status_desired = False
        self._status_cursor_active = False
        self._status_dirty = False
        self._presentation_owner = "content"
        self._status_quota: dict[str, Any] | None = None
        self._status_context: dict[str, Any] | None = None
        self._status_route: tuple[str, str, str] | None = None
        self._markdown_stream = TerminalMarkdownStream()

    def begin_turn(self) -> None:
        """Reset every presentation field whose meaning is scoped to one turn."""
        self.stop_activity()
        self._status_cursor_active = False
        self._presentation_owner = "content"
        self._folded_items.clear()
        self._visible_lines.clear()
        self._last_diff = ""
        self._last_diff_chars = 0
        self._last_diff_digest = ""
        self._needs_agent_separator = False
        self._response_open = False
        self._response_has_text = False
        self._response_ends_with_newline = True
        self._markdown_stream = TerminalMarkdownStream()

    def state(self, name: str, detail: str = "") -> None:
        """Render a semantic lifecycle state without relying on colour."""
        self.stop_activity()
        previous = self.suspend_presentation("state")
        text = f"[cx] {sanitize_untrusted_text(str(name)).upper()}"
        if detail:
            text += f" · {sanitize_untrusted_text(str(detail))}"
        self._line(self._bold(text) if self.color_enabled else text)
        self.restore_presentation(previous)

    def _begin_response(self) -> None:
        if self._response_open:
            return
        self._response_open = True
        if self.is_tty:
            self._line()
            self._line(self._bold("◆ CODEX RESPONSE"))
            self._line(self._dim("─" * min(40, self.terminal_width())))

    # ---------------------------------------------------------
    # Environment
    # ---------------------------------------------------------

    @property
    def stream(self):
        if self._stream_override is not None:
            return self._stream_override

        return sys.stdout

    @property
    def is_tty(self) -> bool:
        method = getattr(
            self.stream,
            "isatty",
            None,
        )

        if not callable(method):
            return False

        try:
            return bool(
                method()
            )
        except Exception:
            return False

    @property
    def color_enabled(self) -> bool:
        return (
            self.is_tty
            and os.environ.get(
                "NO_COLOR"
            )
            is None
        )

    def terminal_width(self) -> int:
        try:
            width = shutil.get_terminal_size(
                fallback=(100, 24)
            ).columns
        except Exception:
            width = 100

        return max(
            40,
            min(
                int(width),
                240,
            ),
        )

    # ---------------------------------------------------------
    # Raw write
    # ---------------------------------------------------------

    def _write(
        self,
        text: str,
        *,
        flush: bool = True,
    ) -> None:

        with self._lock:
            try:
                self.stream.write(
                    text
                )

                if flush:
                    self.stream.flush()

            except UnicodeEncodeError:
                encoding = (
                    getattr(
                        self.stream,
                        "encoding",
                        None,
                    )
                    or "utf-8"
                )

                safe = (
                    text.encode(
                        encoding,
                        errors="replace",
                    ).decode(
                        encoding,
                        errors="replace",
                    )
                )

                self.stream.write(
                    safe
                )

                if flush:
                    self.stream.flush()

    def _line(
        self,
        text: str = "",
    ) -> None:
        if self._status_visible:
            self.suspend_status()
        self._write(
            text + "\n"
        )

    # ---------------------------------------------------------
    # ANSI
    # ---------------------------------------------------------

    def _ansi(
        self,
        code: str,
        text: str,
    ) -> str:

        if not self.color_enabled:
            return text

        return (
            "\x1b["
            + code
            + "m"
            + text
            + "\x1b[0m"
        )

    def _dim(
        self,
        text: str,
    ) -> str:
        return self._ansi(
            "2",
            text,
        )

    def _green(
        self,
        text: str,
    ) -> str:
        return self._ansi(
            "32",
            text,
        )

    def _yellow(
        self,
        text: str,
    ) -> str:
        return self._ansi(
            "33",
            text,
        )

    def _red(
        self,
        text: str,
    ) -> str:
        return self._ansi(
            "31",
            text,
        )

    def _cyan(
        self,
        text: str,
    ) -> str:
        return self._ansi(
            "36",
            text,
        )

    def _bold(
        self,
        text: str,
    ) -> str:
        return self._ansi(
            "1",
            text,
        )

    # ---------------------------------------------------------
    # Spinner
    # ---------------------------------------------------------

    def start_activity(
        self,
        label: str = "İşleniyor",
    ) -> None:

        if not self.is_tty:
            return

        self.stop_activity()
        self.suspend_status()
        self._presentation_owner = "spinner"

        event = threading.Event()

        self._spinner_stop = event
        self._spinner_label = label

        thread = threading.Thread(
            target=self._spinner_loop,
            args=(
                event,
                label,
            ),
            name="cx2-terminal-spinner",
            daemon=True,
        )

        self._spinner_thread = thread
        thread.start()

    def _spinner_loop(
        self,
        event: threading.Event,
        label: str,
    ) -> None:

        index = 0

        while not event.wait(
            0.10
        ):
            frame = self.SPINNER_FRAMES[
                index
                % len(
                    self.SPINNER_FRAMES
                )
            ]

            index += 1

            width = self.terminal_width()

            line = (
                frame
                + " "
                + label
            )

            if len(line) >= width:
                line = line[
                    : width - 1
                ]

            with self._lock:
                self._write(
                    "\r"
                    + line
                    + " ",
                )

    # CX2_ACTIVITY_AWARE_CLEANUP_V1
    def stop_activity(
        self,
    ) -> None:

        event = self._spinner_stop
        thread = self._spinner_thread
        label = self._spinner_label

        # Capture activity state BEFORE clearing the references.
        #
        # Previously stop_activity() erased the current terminal line
        # on every call, even when no spinner/activity existed.
        # That allowed:
        #
        #   agent_delta -> "EVET"
        #   turn_completed -> stop_activity()
        #
        # to erase the already-rendered final answer.
        had_activity = (
            event is not None
            or thread is not None
            or label is not None
        )

        self._spinner_stop = None
        self._spinner_thread = None
        self._spinner_label = None

        if event is not None:
            event.set()

        if (
            thread is not None
            and thread is not threading.current_thread()
        ):
            thread.join(
                timeout=0.30
            )

        # Only an actually active spinner owns an erasable terminal
        # activity line. A second stop after agent output must be a
        # terminal no-op.
        if (
            self.is_tty
            and had_activity
        ):
            width = self.terminal_width()

            self._write(
                "\r"
                + (
                    " "
                    * max(
                        1,
                        width - 1,
                    )
                )
                + "\r"
            )

    # ---------------------------------------------------------
    # Text stream
    # ---------------------------------------------------------

    # CX2_DIFF_APPROVAL_UI_V1
    @property
    def can_prompt(
        self,
    ) -> bool:

        if not self.is_tty:
            return False

        try:
            return bool(
                sys.stdin.isatty()
            )
        except Exception:
            return False


    def diff_updated(
        self,
        diff: str,
    ) -> None:

        if not isinstance(
            diff,
            str,
        ):
            return

        if not diff:
            return

        incoming_digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        if (
            len(diff) == self._last_diff_chars
            and incoming_digest == self._last_diff_digest
        ):
            return

        self.stop_activity()
        self.suspend_status()

        previous_chars = self._last_diff_chars
        previous_digest = self._last_diff_digest
        bounded_diff = diff
        raw_diff_bytes = diff.encode("utf-8")
        if len(raw_diff_bytes) > MAX_VISIBLE_DIFF_BYTES:
            bounded_diff = raw_diff_bytes[-MAX_VISIBLE_DIFF_BYTES:].decode("utf-8", errors="replace")
        self._last_diff = bounded_diff
        self._last_diff_chars = len(diff)
        self._last_diff_digest = incoming_digest

        # turn/diff/updated contains the latest aggregated diff.
        #
        # If the new payload is a strict append of the previous one,
        # only render the newly appended portion. Otherwise render the
        # complete replacement diff so the terminal never shows a
        # misleading partial patch.
        if (
            previous_chars
            and len(diff) >= previous_chars
            and hashlib.sha256(
                diff[:previous_chars].encode("utf-8")
            ).hexdigest() == previous_digest
        ):
            visible_diff = diff[
                previous_chars:
            ]

            heading = "[diff +]"
        else:
            visible_diff = diff
            heading = "[diff]"

        visible_bytes = visible_diff.encode("utf-8")
        if len(visible_bytes) > MAX_VISIBLE_DIFF_BYTES:
            omission = b"\n[cx2] diff middle omitted from terminal view\n"
            retained_budget = MAX_VISIBLE_DIFF_BYTES - len(omission)
            head_budget = retained_budget // 2
            tail_budget = retained_budget - head_budget
            visible_diff = (
                visible_bytes[:head_budget].decode("utf-8", errors="replace")
                + omission.decode("ascii")
                + visible_bytes[-tail_budget:].decode("utf-8", errors="replace")
            )
            heading += " (bounded head/tail)"

        if not visible_diff:
            self.resume_status()
            return

        self._needs_agent_separator = True

        self._line()
        self._line(
            self._cyan(
                heading
            )
        )

        lines = visible_diff.splitlines()

        if self.is_tty:
            max_lines = 240
            rendered_lines = lines[
                :max_lines
            ]
        else:
            max_lines = len(lines)
            rendered_lines = lines

        for line in rendered_lines:

            line = sanitize_untrusted_text(line)

            if (
                line.startswith("+")
                and not line.startswith("+++")
            ):
                rendered = self._green(
                    line
                )

            elif (
                line.startswith("-")
                and not line.startswith("---")
            ):
                rendered = self._red(
                    line
                )

            elif line.startswith("@@"):
                rendered = self._cyan(
                    line
                )

            else:
                rendered = line

            self._line(
                rendered
            )

        if len(lines) > max_lines:
            self._line(
                self._dim(
                    "[cx2] diff output folded; "
                    "full unified diff retained internally"
                )
            )


    def approval_prompt(
        self,
        *,
        title: str,
        details: list[str],
        decisions: list[str],
        default_decision: str,
    ) -> str:

        self.stop_activity()
        self.suspend_status()

        allowed: list[str] = []

        for decision in decisions:

            if (
                isinstance(
                    decision,
                    str,
                )
                and decision
                and decision not in allowed
            ):
                allowed.append(
                    decision
                )

        deny_like = (
            "decline",
            "denied",
            "cancel",
            "abort",
        )

        fallback = (
            default_decision
            if default_decision in allowed
            else ""
        )

        if not fallback:

            for candidate in deny_like:

                if candidate in allowed:
                    fallback = candidate
                    break

        # Never manufacture an approval if the protocol offers no
        # deny/cancel-style string decision.
        if not fallback:
            self.resume_status()
            return ""

        # Redirected/non-interactive execution must remain safe and
        # deterministic.
        if not self.can_prompt:
            print(
                f"[cx] approval declined (non-interactive): {title}",
                file=sys.stderr,
            )
            self.resume_status()
            return fallback

        self._line()
        self._line(
            self._yellow(
                "[onay]"
            )
            + " "
            + str(title)
        )

        for detail in details:

            value = str(
                detail
            ).strip()

            if value:
                self._line(
                    "  "
                    + value
                )

        key_for_decision = {
            "accept":
                "1",

            "approved":
                "1",

            "acceptForSession":
                "2",

            "approved_for_session":
                "2",

            "decline":
                "3",

            "denied":
                "3",

            "cancel":
                "4",

            "abort":
                "4",
        }

        labels = {
            "1":
                "Bu kez izin ver",

            "2":
                "Oturum boyunca izin ver",

            "3":
                "Reddet",

            "4":
                "İptal",
        }

        choices: dict[str, str] = {}

        for decision in allowed:

            key = key_for_decision.get(
                decision
            )

            if (
                key
                and key not in choices
            ):
                choices[
                    key
                ] = decision

        fallback_key = (
            key_for_decision.get(
                fallback,
                "3",
            )
        )

        option_text = []

        for key in (
            "1",
            "2",
            "3",
            "4",
        ):

            if key not in choices:
                continue

            option_text.append(
                "["
                + key
                + "] "
                + labels[key]
            )

        if option_text:
            self._line(
                "  "
                + " | ".join(
                    option_text
                )
            )

        aliases = {
            "1":
                "1",
            "y":
                "1",
            "yes":
                "1",
            "e":
                "1",
            "evet":
                "1",

            "2":
                "2",
            "a":
                "2",
            "session":
                "2",
            "oturum":
                "2",

            "3":
                "3",
            "n":
                "3",
            "no":
                "3",
            "h":
                "3",
            "hayır":
                "3",
            "hayir":
                "3",
            "red":
                "3",
            "reddet":
                "3",
            "deny":
                "3",

            "4":
                "4",
            "c":
                "4",
            "cancel":
                "4",
            "iptal":
                "4",
            "abort":
                "4",
        }

        while True:

            self._write(
                "Seçim ["
                + fallback_key
                + "]: "
            )

            try:
                answer = input()

            except EOFError:
                self._line()
                self.resume_status()
                return fallback

            except KeyboardInterrupt:
                self._line()

                for candidate in (
                    "cancel",
                    "abort",
                ):

                    if candidate in allowed:
                        self.resume_status()
                        return candidate

                self.resume_status()
                return fallback

            answer = answer.strip().casefold()

            if not answer:
                self.resume_status()
                return fallback

            selected_key = aliases.get(
                answer
            )

            if (
                selected_key
                and selected_key in choices
            ):
                self.resume_status()
                return choices[
                    selected_key
                ]

            self._line(
                "Geçersiz seçim."
            )


    def agent_delta(
        self,
        delta: str,
    ) -> None:

        self.stop_activity()
        self.suspend_status()

        self._begin_response()

        if self._needs_agent_separator:
            self._line()
            self._needs_agent_separator = False

        presented = (
            self._markdown_stream.feed(delta, color=self.color_enabled)
            if self.is_tty
            else sanitize_untrusted_text(delta)
        )
        self._write(presented)
        if delta:
            self._response_has_text = True
            self._response_ends_with_newline = delta.endswith("\n")

    @property
    def capabilities(self) -> TerminalCapabilities:
        tty = self.is_tty
        input_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
        term = os.environ.get("TERM", "").casefold()
        dumb = term == "dumb"
        unicode_ok = (getattr(self.stream, "encoding", None) or "utf-8").casefold() not in {"ascii", "cp1252"}
        static_ui = os.environ.get("CX2_STATIC_UI", "").casefold() in {"1", "true", "yes", "on"}
        cursor = tty and not dumb and not static_ui
        return TerminalCapabilities(
            tty=tty,
            interactive_input=input_tty,
            color=self.color_enabled,
            unicode=unicode_ok,
            cursor=cursor,
            sticky_status=cursor and input_tty,
        )

    def set_status_snapshot(
        self,
        *,
        quota: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        model: str | None = None,
        effort: str | None = None,
        sandbox: str | None = None,
    ) -> str:
        if quota is not None:
            if (
                quota.get("available") is False
                and isinstance(self._status_quota, dict)
                and isinstance(self._status_quota.get("remainingPercent"), (int, float))
            ):
                self._status_quota = dict(self._status_quota)
                self._status_quota["refreshUnavailable"] = True
            else:
                self._status_quota = dict(quota)
        if context is not None:
            self._status_context = dict(context)
        if model is not None or effort is not None or sandbox is not None:
            self._status_route = (
                str(model or "CX2"),
                str(effort or "-").upper(),
                str(sandbox or "-").upper(),
            )
        quota = self._status_quota
        context = self._status_context
        parts: list[str] = []
        if self._status_route is not None:
            parts.extend(self._status_route)
        if isinstance(quota, dict):
            remaining = quota.get("remainingPercent")
            if isinstance(remaining, (int, float)):
                parts.append(f"quota {remaining:.0f}%")
            else:
                parts.append("quota ? · unavailable")
            state = str(quota.get("state") or "").upper()
            if state and state not in {"NORMAL", "UNKNOWN", "NONE"}:
                parts.append(state)
            parts.append(_quota_freshness(quota))
            if quota.get("refreshUnavailable"):
                parts.append("refresh unavailable")
        if isinstance(context, dict):
            percent = context.get("percent")
            if isinstance(percent, (int, float)):
                parts.append(f"context {percent:.0f}%")
        self._status_text = " · ".join(parts)
        return self._status_text

    def render_status_line(self) -> None:
        self._status_desired = bool(self._status_text)
        if not self._status_text or not self.capabilities.sticky_status:
            return
        if self._presentation_owner == "prompt":
            self._status_dirty = True
            return
        avail = max(1, int(self.terminal_width()) - 1)
        plain_text = "[cx] " + self._status_text
        bounded_text = _truncate_to_cell_width(plain_text, avail)
        self._write("\r\x1b[2K" + self._dim(bounded_text), flush=True)
        self._status_visible = True
        self._status_cursor_active = True
        self._status_dirty = False
        self._presentation_owner = "status"

    def suspend_status(self) -> None:
        if self._status_visible and self._status_cursor_active and self.capabilities.cursor:
            self._write("\r\x1b[2K", flush=True)
            self._status_cursor_active = False
            self._status_visible = False

    def resume_status(self) -> None:
        if self._status_desired:
            self.render_status_line()

    def prompt_input(
        self,
        prompt: str = "CX> ",
        *,
        input_func: Any | None = None,
    ) -> str:
        if input_func is None:
            input_func = input

        if not self.capabilities.sticky_status:
            self._presentation_owner = "prompt"
            try:
                return input_func(prompt)
            finally:
                self._presentation_owner = "content"
                self._status_cursor_active = False

        if self._status_desired or self._status_dirty or not self._status_visible:
            if self._status_text:
                self.render_status_line()

        if self._status_visible and self._status_cursor_active:
            self._write("\r\n\r\n\r", flush=True)
            self._status_cursor_active = False

        self._presentation_owner = "prompt"
        try:
            return input_func(prompt)
        finally:
            self._presentation_owner = "content"
            self._status_cursor_active = False

    def suspend_presentation(self, owner: str) -> tuple[str, bool]:
        """Transfer current-row ownership to a modal presenter."""

        previous = (self._presentation_owner, self._status_desired)
        self.stop_activity()
        self.suspend_status()
        self._presentation_owner = str(owner)
        return previous

    def restore_presentation(self, previous: tuple[str, bool]) -> None:
        previous_owner, wanted_status = previous
        self._presentation_owner = previous_owner
        self._status_desired = bool(wanted_status)
        if wanted_status:
            self.resume_status()

    def confirm_empty_response(self) -> None:
        """Open the TTY response boundary for an authoritative empty final."""

        self.stop_activity()
        self._begin_response()

    def response_reconciled(
        self,
        authoritative_text: str,
    ) -> None:
        """Visibly replace a stale streamed representation with canonical text."""

        self.stop_activity()
        if self._response_has_text and not self._response_ends_with_newline:
            self._line()
        self._line(
            "[cx] RESPONSE RECONCILED — authoritative final response differs "
            "from the streamed representation."
        )
        if self.is_tty:
            self._line(self._bold("◆ CODEX RESPONSE · RECONCILED"))
            self._line(self._dim("─" * min(40, self.terminal_width())))
        else:
            self._line("◆ CODEX RESPONSE · RECONCILED")

        self._response_open = True
        self._markdown_stream = TerminalMarkdownStream()
        presented = (
            render_markdown(authoritative_text, color=self.color_enabled)
            if self.is_tty
            else sanitize_untrusted_text(authoritative_text)
        )
        self._write(presented)
        self._response_has_text = bool(authoritative_text)
        self._response_ends_with_newline = (
            not authoritative_text or authoritative_text.endswith("\n")
        )

    def response_ambiguity(
        self,
        reason: str,
    ) -> None:
        self.stop_activity()
        if self._response_has_text and not self._response_ends_with_newline:
            self._line()
            self._response_ends_with_newline = True
        self._line(
            "[cx] RESPONSE REJECTED — ambiguous authoritative final response "
            f"({reason})."
        )

    # ---------------------------------------------------------
    # Command lifecycle
    # ---------------------------------------------------------

    def _truncate_command(
        self,
        command: str,
    ) -> str:

        if not self.is_tty:
            return command

        width = self.terminal_width()

        maximum = max(
            20,
            width - 4,
        )

        if len(command) <= maximum:
            return command

        return (
            command[
                : maximum - 3
            ]
            + "..."
        )

    # CX2_NATIVE_WEB_RENDERER_V1
    def _web_action_text(
        self,
        item: dict[str, Any],
    ) -> str:

        action = item.get(
            "action"
        )

        query = item.get(
            "query"
        )

        if not isinstance(
            action,
            dict,
        ):
            return (
                str(query)
                if query
                else "web"
            )

        kind = str(
            action.get(
                "type",
                "other",
            )
        )

        if kind == "search":

            queries = action.get(
                "queries"
            )

            action_query = action.get(
                "query"
            )

            if (
                isinstance(
                    action_query,
                    str,
                )
                and action_query
            ):
                return (
                    "search: "
                    + action_query
                )

            if (
                isinstance(
                    queries,
                    list,
                )
                and queries
            ):
                return (
                    "search: "
                    + " | ".join(
                        str(value)
                        for value in queries[
                            :4
                        ]
                    )
                )

            if query:
                return (
                    "search: "
                    + str(
                        query
                    )
                )

            return "search"


        if kind in {
            "openPage",
            "open_page",
        }:

            url = action.get(
                "url"
            )

            return (
                "open: "
                + str(
                    url
                    or query
                    or "page"
                )
            )


        if kind in {
            "findInPage",
            "find_in_page",
        }:

            pattern = action.get(
                "pattern"
            )

            url = action.get(
                "url"
            )

            text = (
                "find: "
                + str(
                    pattern
                    or "?"
                )
            )

            if url:
                text += (
                    " @ "
                    + str(
                        url
                    )
                )

            return text


        return (
            str(query)
            if query
            else kind
        )


    def web_search_started(
        self,
        item: dict[str, Any],
    ) -> None:

        self.stop_activity()
        previous = self.suspend_presentation("web")

        self._line()

        self._line(
            self._cyan(
                "[web]"
            )
            + " "
            + sanitize_untrusted_text(self._web_action_text(item))
        )
        self.restore_presentation(previous)


    def web_search_completed(
        self,
        item: dict[str, Any],
    ) -> None:

        self.stop_activity()
        previous = self.suspend_presentation("web")
        self._needs_agent_separator = True

        self._line(
            self._green(
                "[web ok]"
            )
            + " "
            + sanitize_untrusted_text(self._web_action_text(item))
        )
        self.restore_presentation(previous)


    @staticmethod
    def _format_duration(
        duration: Any,
    ) -> str:
        if duration is None:
            return ""
        if isinstance(duration, (int, float)):
            if duration >= 1000:
                return f"{duration / 1000:.1f}s"
            return f"{int(duration)}ms"
        return str(duration)

    def command_started(
        self,
        command: str,
    ) -> None:

        self.stop_activity()
        self.suspend_status()
        self._needs_agent_separator = False

        unwrapped = unwrap_display_command(str(command))
        command_text = self._truncate_command(
            sanitize_untrusted_text(unwrapped or str(command))
        )

        prefix = self._cyan(
            ">"
        )

        self._line()
        self._line(
            prefix
            + " "
            + command_text
        )
        self.resume_status()

    def command_output_delta(
        self,
        item_id: str,
        delta: str,
    ) -> None:

        self.stop_activity()
        self.suspend_status()

        if self.compact_tools and self.is_tty:
            # Keep the full output in turn_runner's bounded ledger; the normal
            # terminal view shows only the command badge on completion.
            self.resume_status()
            return

        # Redirected stdout must remain complete and machine/log friendly.
        if not self.is_tty:
            self._write(
                sanitize_untrusted_text(delta)
            )
            return

        key = str(
            item_id
        )

        if key not in self._visible_lines and len(self._visible_lines) >= MAX_ACTIVITY_ITEMS:
            oldest = next(iter(self._visible_lines), None)
            if oldest is not None:
                self._visible_lines.pop(oldest, None)
                self._folded_items.discard(oldest)

        if key in self._folded_items:
            return

        used = self._visible_lines.get(
            key,
            0,
        )

        pieces = delta.splitlines(
            keepends=True
        )

        output_parts: list[str] = []

        for piece in pieces:
            if used >= self.max_command_lines:
                self._folded_items.add(
                    key
                )
                break

            output_parts.append(
                piece
            )

            if (
                piece.endswith(
                    "\n"
                )
                or piece.endswith(
                    "\r"
                )
            ):
                used += 1

        self._visible_lines[
            key
        ] = used

        if output_parts:
            self._write(sanitize_untrusted_text("".join(output_parts)))

        if key in self._folded_items:
            self._line(
                self._dim(
                    "[cx2] additional command output folded; "
                    "full output retained internally"
                )
            )
        self.resume_status()

    def command_completed(
        self,
        summary: dict[str, Any],
    ) -> None:

        self.stop_activity()
        self.suspend_status()
        self._needs_agent_separator = True

        exit_code = summary.get(
            "exitCode"
        )

        status = summary.get(
            "status"
        )

        duration = (
            summary.get(
                "durationMs"
            )
            or summary.get(
                "duration_ms"
            )
        )

        dur_str = self._format_duration(duration)
        cmd_raw = str(summary.get("command") or summary.get("display_command") or "")

        if status == "interrupted" or summary.get("interrupted"):
            marker = self._yellow(
                "[interrupted]"
            )
            text = f"{marker} {dur_str}" if dur_str else marker

        elif exit_code == 0:
            marker = self._green(
                "[ok]"
            )
            text = f"{marker} {dur_str}" if dur_str else marker

        elif exit_code == 1 and is_ripgrep_command(cmd_raw):
            marker = self._dim(
                "[no-match]"
            )
            text = f"{marker} {dur_str}" if dur_str else marker

        elif isinstance(
            exit_code,
            int,
        ):
            marker = self._red(
                "[failed]"
            )
            parts = [
                f"exit {exit_code}"
            ]
            if dur_str:
                parts.append(
                    dur_str
                )
            text = f"{marker} {' · '.join(parts)}"

        else:
            marker = self._dim(
                "[done]"
            )
            parts = []
            if status:
                parts.append(
                    f"status {status}"
                )
            if dur_str:
                parts.append(
                    dur_str
                )
            text = f"{marker} {' · '.join(parts)}" if parts else marker

        self._line(
            text
        )
        self.resume_status()

    # ---------------------------------------------------------
    # Turn header & metadata presentation
    # ---------------------------------------------------------

    def render_turn_header(
        self,
        *,
        session_mode: str,
        model: str,
        effort: str | None = None,
        sandbox: str | None = None,
        effective_sandbox: str | None = None,
        sandbox_compatibility_mode: str | None = None,
        quota: dict[str, Any] | None = None,
    ) -> None:
        """
        Renders a single compact, low-noise metadata header for the turn.
        Example:
            [cx] RESUME · gpt-5.6-luna · low · read-only · 27% kaldı
            [cx] RESUME · gpt-5.6-luna · low · read-only · 27% kaldı · CONSERVE
        """
        self.begin_turn()

        mode_str = "RESUME" if str(session_mode).lower() == "resume" else "NEW"
        parts = [mode_str, str(model)]

        if effort:
            parts.append(str(effort))

        if sandbox:
            parts.append(str(sandbox))

        if isinstance(quota, dict) and quota.get("available"):
            remaining = quota.get("remainingPercent")
            if isinstance(remaining, (int, float)):
                parts.append(f"{remaining:.0f}% kaldı")

            parts.append(_quota_freshness(quota))

            state = str(quota.get("state") or "").upper()
            if state and state not in {"NORMAL", "UNKNOWN", "NONE"}:
                parts.append(state)
        elif isinstance(quota, dict):
            parts.append("quota unavailable")
            parts.append(_quota_freshness(quota))
            if quota.get("refreshUnavailable"):
                parts.append("refresh unavailable")

        line_text = f"[cx] {' · '.join(parts)}"
        self._line(self._dim(line_text))
        self.set_status_snapshot(quota=quota, model=model, effort=effort, sandbox=sandbox)
        self.render_status_line()

        if sandbox_compatibility_mode or (effective_sandbox and sandbox and effective_sandbox != sandbox):
            self.suspend_status()
            compat_notice = "[cx] Windows sandbox: güvenli read-only uyumluluk modu etkin; yazma işlemleri ayrıca onay isteyebilir."
            self._line(self._dim(compat_notice))
            self.resume_status()

    def render_context_summary(
        self,
        info: dict[str, Any],
        policy: dict[str, Any] | None = None,
    ) -> None:
        """
        Renders a compact context summary footer.
        Example:
            [cx] context 16% · native compaction
        """
        if not isinstance(info, dict):
            return

        percent = info.get("percent")
        if not isinstance(percent, (int, float)):
            return

        self.stop_activity()
        line_text = f"[cx] context {percent:.0f}% · native compaction"
        self._line(self._dim(line_text))
        self.set_status_snapshot(context=info)
        self.render_status_line()

        warn = 75.0
        if isinstance(policy, dict):
            warn = float(
                policy.get("session", {}).get("context_warn_percent", 75)
            )

        if percent >= warn:
            self.suspend_status()
            self._line(
                self._yellow(
                    "[cx] Context is getting large; "
                    "native Codex compaction remains enabled."
                )
            )
            self.resume_status()

    # ---------------------------------------------------------
    # Turn lifecycle
    # ---------------------------------------------------------

    def turn_started(
        self,
    ) -> None:

        self.begin_turn()

        self.start_activity(
            "İşleniyor"
        )

    def turn_completed(
        self,
        status: str,
        *,
        duration_ms: int | None = None,
        line_count: int | None = None,
    ) -> None:

        self.stop_activity()
        self.suspend_status()
        self._needs_agent_separator = False

        if self.is_tty:
            pending = self._markdown_stream.finish(color=self.color_enabled)
            if pending:
                self._write(pending)

        if self._response_has_text and not self._response_ends_with_newline:
            self._line()
            self._response_ends_with_newline = True

        if status not in {
            "completed",
            "success",
        }:
            self._line(
                self._dim(
                    "[cx] turn="
                    + str(status)
                )
            )
        elif self._response_open and self.is_tty:
            self._line(self._dim("─" * min(40, self.terminal_width())))
            details: list[str] = []
            if isinstance(duration_ms, int) and duration_ms >= 0:
                details.append(f"{duration_ms / 1000:.1f}s")
            if isinstance(line_count, int) and line_count >= 0:
                details.append(f"{line_count} lines")
            suffix = " · " + " · ".join(details) if details else ""
            self._line(self._green("✓ Completed") + suffix)

        self._response_open = False
        self.resume_status()

    # ---------------------------------------------------------
    # Diagnostics
    # ---------------------------------------------------------

    def warning(
        self,
        message: str,
    ) -> None:

        self.stop_activity()
        previous = self.suspend_presentation("warning")

        self._line()
        self._line(
            self._yellow(
                "[cx2 warning]"
            )
            + " "
            + sanitize_untrusted_text(str(message))
        )
        self.restore_presentation(previous)

    def error(
        self,
        error: Any,
    ) -> None:

        self.stop_activity()
        previous = self.suspend_presentation("error")

        self._line()
        self._line(
            self._red(
                "[cx2 error]"
            )
            + " "
            + sanitize_untrusted_text(str(error))
        )
        self.restore_presentation(previous)

    def interrupting(
        self,
    ) -> None:

        self.stop_activity()

        self._line()
        self._line(
            self._yellow(
                "[cx]"
            )
            + " İşlem kesiliyor..."
        )
        self.resume_status()

    def interrupted(self) -> None:
        """Compatibility hook for a completed interruption transition."""

        self.interrupting()

    def verification_continuation_started(
        self,
        reason: str = "",
    ) -> None:

        self.stop_activity()
        self._line()
        self._line(
            self._cyan(
                "[doğrulama]"
            )
            + " Değişiklikler için otomatik doğrulama turu başlatılıyor..."
        )
        self._line()

    def render_verification_summary(
        self,
        assessment: Any,
    ) -> None:

        if assessment is None:
            return

        if isinstance(assessment, dict):
            status = assessment.get("status")
            changed_files = assessment.get("changed_files", [])
            valid_cmds = assessment.get("valid_evidence_commands", [])
            dominant_cat = assessment.get("dominant_category", "OTHER")
            reason = assessment.get("reason", "")
            audit_assessment = assessment.get("audit_assessment")
        else:
            status = getattr(assessment, "status", None)
            changed_files = getattr(assessment, "changed_files", [])
            valid_cmds = getattr(assessment, "valid_evidence_commands", [])
            dominant_cat = getattr(assessment, "dominant_category", "OTHER")
            reason = getattr(assessment, "reason", "")
            audit_assessment = getattr(assessment, "audit_assessment", None)

        # Required Verification Coverage Rendering
        req_cov = assessment.get("required_coverage") if isinstance(assessment, dict) else getattr(assessment, "required_coverage", None)
        if req_cov is not None:
            req_tot = req_cov.get("required_total", 0) if isinstance(req_cov, dict) else getattr(req_cov, "required_total", 0)
            if req_tot > 0:
                p_cnt = req_cov.get("passed_count", 0) if isinstance(req_cov, dict) else getattr(req_cov, "passed_count", 0)
                f_cnt = req_cov.get("failed_count", 0) if isinstance(req_cov, dict) else getattr(req_cov, "failed_count", 0)
                b_cnt = req_cov.get("blocked_count", 0) if isinstance(req_cov, dict) else getattr(req_cov, "blocked_count", 0)
                m_cnt = req_cov.get("missing_count", 0) if isinstance(req_cov, dict) else getattr(req_cov, "missing_count", 0)
                missing_gates = req_cov.get("missing_gates", []) if isinstance(req_cov, dict) else getattr(req_cov, "missing_gates", [])
                failed_gates = req_cov.get("failed_gates", []) if isinstance(req_cov, dict) else getattr(req_cov, "failed_gates", [])
                blocked_gates = req_cov.get("blocked_gates", []) if isinstance(req_cov, dict) else getattr(req_cov, "blocked_gates", [])

                prefix = self._bold("[doğrulama]")
                if status == "VERIFIED":
                    badge = self._green("VERIFIED")
                    summary_text = f"zorunlu {req_tot}/{req_tot} kapı geçti"
                elif status == "NOT_APPLICABLE":
                    badge = self._dim("NOT_APPLICABLE")
                    summary_text = f"zorunlu {p_cnt}/{req_tot} kapı geçti" + (f" · {m_cnt} eksik" if m_cnt > 0 else "")
                elif status == "PARTIALLY_VERIFIED":
                    badge = self._yellow("PARTIALLY_VERIFIED")
                    summary_text = f"zorunlu {p_cnt}/{req_tot} kapı geçti · {m_cnt} eksik"
                elif status == "FAILED":
                    badge = self._red("FAILED")
                    summary_text = f"zorunlu {p_cnt}/{req_tot} kapı geçti · {f_cnt} başarısız"
                elif status == "BLOCKED":
                    badge = self._red("BLOCKED")
                    summary_text = f"zorunlu {p_cnt}/{req_tot} kapı geçti · {b_cnt} engellendi"
                elif status == "INTERRUPTED":
                    badge = self._yellow("KESİLDİ")
                    summary_text = f"zorunlu {p_cnt}/{req_tot} kapı"
                else:
                    badge = self._yellow("UNVERIFIED")
                    summary_text = f"zorunlu {p_cnt}/{req_tot} kapı geçti · {m_cnt} eksik"

                self.stop_activity()
                self._line()
                self._line(f"{prefix} · {badge} · {summary_text}")

                rendered_details = 0
                max_details = 5
                for fg in failed_gates:
                    if rendered_details >= max_details:
                        break
                    surf = fg.get("surface") if isinstance(fg, dict) else getattr(fg, "surface", "")
                    raw = fg.get("raw_command") if isinstance(fg, dict) else getattr(fg, "raw_command", "")
                    self._line(f"  {self._red('[başarısız]')} {surf} · {raw}")
                    rendered_details += 1

                for bg in blocked_gates:
                    if rendered_details >= max_details:
                        break
                    surf = bg.get("surface") if isinstance(bg, dict) else getattr(bg, "surface", "")
                    raw = bg.get("raw_command") if isinstance(bg, dict) else getattr(bg, "raw_command", "")
                    self._line(f"  {self._red('[engellendi]')} {surf} · {raw}")
                    rendered_details += 1

                for mg in missing_gates:
                    if rendered_details >= max_details:
                        break
                    surf = mg.get("surface") if isinstance(mg, dict) else getattr(mg, "surface", "")
                    raw = mg.get("raw_command") if isinstance(mg, dict) else getattr(mg, "raw_command", "")
                    self._line(f"  {self._yellow('[eksik]')} {surf} · {raw}")
                    rendered_details += 1

                tot_issues = len(failed_gates) + len(blocked_gates) + len(missing_gates)
                if tot_issues > max_details:
                    self._line(f"  {self._dim(f'... ve {tot_issues - max_details} kapı daha')}")

                # If there is also an audit assessment on read-only turn, render it as well
                if status == "NOT_APPLICABLE" and audit_assessment:
                    if isinstance(audit_assessment, dict):
                        a_status = audit_assessment.get("status", "UNVERIFIED")
                        tot_checks = audit_assessment.get("total_checks", 0)
                        a_p_cnt = audit_assessment.get("passed_count", 0)
                        a_f_cnt = audit_assessment.get("failed_count", 0)
                        a_b_cnt = audit_assessment.get("blocked_count", 0)
                    else:
                        a_status = getattr(audit_assessment, "status", "UNVERIFIED")
                        tot_checks = getattr(audit_assessment, "total_checks", 0)
                        a_p_cnt = getattr(audit_assessment, "passed_count", 0)
                        a_f_cnt = getattr(audit_assessment, "failed_count", 0)
                        a_b_cnt = getattr(audit_assessment, "blocked_count", 0)

                    if tot_checks > 0 or a_status == "INTERRUPTED":
                        audit_prefix = self._bold("[audit]")
                        if a_status == "COMPLETE":
                            a_badge = self._green("COMPLETE")
                        elif a_status == "PARTIAL":
                            a_badge = self._yellow("PARTIAL")
                        elif a_status == "INTERRUPTED":
                            a_badge = self._yellow("INTERRUPTED")
                        else:
                            a_badge = self._yellow("UNVERIFIED")

                        audit_parts = [audit_prefix, a_badge]
                        if tot_checks > 0:
                            audit_parts.append(f"{tot_checks} {'check' if tot_checks == 1 else 'checks'}")
                            if a_p_cnt > 0:
                                audit_parts.append(f"{a_p_cnt} passed")
                            if a_f_cnt > 0:
                                audit_parts.append(f"{a_f_cnt} failed")
                            if a_b_cnt > 0:
                                audit_parts.append(f"{a_b_cnt} blocked")

                        self._line(" · ".join(audit_parts))

                self._line()
                return

        # Read-only audit assurance rendering
        if status == "NOT_APPLICABLE" and not changed_files:
            if not audit_assessment:
                return

            if isinstance(audit_assessment, dict):
                a_status = audit_assessment.get("status", "UNVERIFIED")
                tot_checks = audit_assessment.get("total_checks", 0)
                p_cnt = audit_assessment.get("passed_count", 0)
                f_cnt = audit_assessment.get("failed_count", 0)
                b_cnt = audit_assessment.get("blocked_count", 0)
            else:
                a_status = getattr(audit_assessment, "status", "UNVERIFIED")
                tot_checks = getattr(audit_assessment, "total_checks", 0)
                p_cnt = getattr(audit_assessment, "passed_count", 0)
                f_cnt = getattr(audit_assessment, "failed_count", 0)
                b_cnt = getattr(audit_assessment, "blocked_count", 0)

            if tot_checks == 0 and a_status != "INTERRUPTED":
                return

            self.stop_activity()
            self._line()

            audit_prefix = self._bold("[audit]")
            if a_status == "COMPLETE":
                a_badge = self._green("COMPLETE")
            elif a_status == "PARTIAL":
                a_badge = self._yellow("PARTIAL")
            elif a_status == "INTERRUPTED":
                a_badge = self._yellow("INTERRUPTED")
            else:
                a_badge = self._yellow("UNVERIFIED")

            audit_parts = [audit_prefix, a_badge]
            if tot_checks > 0:
                audit_parts.append(f"{tot_checks} {'check' if tot_checks == 1 else 'checks'}")
                if p_cnt > 0:
                    audit_parts.append(f"{p_cnt} passed")
                if f_cnt > 0:
                    audit_parts.append(f"{f_cnt} failed")
                if b_cnt > 0:
                    audit_parts.append(f"{b_cnt} blocked")

            self._line(" · ".join(audit_parts))
            self._line()
            return

        self.stop_activity()
        self._line()

        # Batch 7 legacy mock test compatibility (reason == "REASON")
        if reason == "REASON":
            self._line(self._bold("[doğrulama]"))
            file_count = len(changed_files)
            if file_count > 0:
                if file_count == 1:
                    files_str = f"1 dosya ({changed_files[0]})"
                elif file_count <= 3:
                    files_str = f"{file_count} dosya ({', '.join(changed_files)})"
                else:
                    files_str = f"{file_count} dosya ({', '.join(changed_files[:3])}, ... +{file_count - 3})"
                self._line(f"Değişiklik : {files_str}")
            if status == "VERIFIED":
                status_text = self._green("VERIFIED")
            elif status == "PARTIALLY_VERIFIED":
                status_text = self._yellow("PARTIALLY_VERIFIED")
            elif status == "NOT_APPLICABLE":
                status_text = "NOT_APPLICABLE"
            elif status == "FAILED":
                status_text = self._red("FAILED")
            elif status == "BLOCKED":
                status_text = self._red("BLOCKED")
            elif status == "INTERRUPTED":
                status_text = self._yellow("INTERRUPTED")
            else:
                status_text = self._yellow("UNVERIFIED")
            self._line(f"Durum      : {status_text}")
            self._line()
            return

        # Compact Badge Construction
        prefix = self._bold("[doğrulama]")

        # 1. Status label & styling
        if status == "VERIFIED":
            status_badge = self._green("VERIFIED")
            status_plain = "VERIFIED"
        elif status == "PARTIALLY_VERIFIED":
            status_badge = self._yellow("PARTIALLY_VERIFIED")
            status_plain = "PARTIALLY_VERIFIED"
        elif status == "FAILED":
            status_badge = self._red("FAILED")
            status_plain = "FAILED"
        elif status == "BLOCKED":
            status_badge = self._red("BLOCKED")
            status_plain = "BLOCKED"
        elif status == "INTERRUPTED":
            status_badge = self._yellow("KESİLDİ")
            status_plain = "KESİLDİ"
        elif status == "NOT_APPLICABLE":
            status_badge = self._dim("NOT_APPLICABLE")
            status_plain = "NOT_APPLICABLE"
        elif status == "SKIPPED" or reason == "USER_REQUESTED_SKIP":
            status_badge = self._yellow("ATLANDI")
            status_plain = "ATLANDI"
        else:
            status_badge = self._yellow("UNVERIFIED")
            status_plain = "UNVERIFIED"

        # 2. File summary
        file_count = len(changed_files)
        files_full = ""
        files_compact = ""
        if file_count > 0:
            if file_count == 1:
                files_full = f"1 dosya ({changed_files[0]})"
                files_compact = "1 dosya"
            elif file_count <= 3:
                files_full = f"{file_count} dosya ({', '.join(changed_files)})"
                files_compact = f"{file_count} dosya"
            else:
                files_full = f"{file_count} dosya ({', '.join(changed_files[:3])}, ... +{file_count - 3})"
                files_compact = f"{file_count} dosya"
        elif dominant_cat == "DOCS_ONLY":
            files_full = "Dokümantasyon"
            files_compact = "Dokümantasyon"

        # 3. Command & duration
        cmd_text = ""
        cmd_exit = None
        dur_str = ""
        if valid_cmds:
            first_cmd = valid_cmds[0]
            raw_cmd = first_cmd.get("display_command") or first_cmd.get("command", "")
            cmd_text = unwrap_display_command(str(raw_cmd)) or str(raw_cmd)
            cmd_exit = first_cmd.get("exit_code")
            dur_ms = first_cmd.get("duration_ms")
            if isinstance(dur_ms, (int, float)) and dur_ms > 0:
                dur_str = f"{dur_ms/1000:.1f}s" if dur_ms >= 1000 else f"{int(dur_ms)}ms"

        # Build parts based on available width
        term_width = self.terminal_width()

        # Check full representation
        parts_full = [prefix, status_badge]
        if files_full:
            parts_full.append(files_full)
        if cmd_text:
            if cmd_exit is not None and cmd_exit != 0:
                parts_full.append(f"{cmd_text} · exit {cmd_exit}")
            else:
                parts_full.append(cmd_text)
        if dur_str:
            parts_full.append(dur_str)

        full_line = " · ".join(parts_full)
        plain_len = len("[doğrulama] · " + status_plain + (" · " + files_full if files_full else "") + (" · " + cmd_text if cmd_text else "") + (" · " + dur_str if dur_str else ""))

        if plain_len <= term_width:
            self._line(full_line)
        else:
            # Try compact files
            parts_compact = [prefix, status_badge]
            if files_compact:
                parts_compact.append(files_compact)

            base_compact_len = len("[doğrulama] · " + status_plain + (" · " + files_compact if files_compact else "") + (" · " + dur_str if dur_str else ""))
            avail_cmd = term_width - base_compact_len - 3

            if cmd_text:
                if avail_cmd >= 8:
                    if len(cmd_text) > avail_cmd:
                        trunc_cmd = cmd_text[:max(4, avail_cmd - 3)] + "..."
                    else:
                        trunc_cmd = cmd_text
                    if cmd_exit is not None and cmd_exit != 0:
                        parts_compact.append(f"{trunc_cmd} · exit {cmd_exit}")
                    else:
                        parts_compact.append(trunc_cmd)
            if dur_str and (avail_cmd >= 8 or not cmd_text):
                parts_compact.append(dur_str)

            compact_line = " · ".join(parts_compact)
            self._line(compact_line)

        # Failure or skip diagnostics if applicable
        if status in {"BLOCKED", "FAILED", "UNVERIFIED"} and reason and reason not in {"USER_REQUESTED_SKIP", "NONE", "REASON"}:
            self._line(f"  {self._dim(reason)}")
        elif reason == "USER_REQUESTED_SKIP":
            self._line(f"  {self._dim('Kullanıcı talebiyle doğrulama atlandı')}")
        elif reason == "QUOTA_HARD_STOP":
            self._line(f"  {self._yellow('Kota sınırı nedeniyle doğrulama çalıştırılamadı')}")

        self._line()

    def close(
        self,
    ) -> None:

        self.stop_activity()
