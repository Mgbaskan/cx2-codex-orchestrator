from __future__ import annotations

from dataclasses import (
    asdict,
    dataclass,
    field,
)

from pathlib import Path

import hashlib
import sys
import time

from typing import (
    Any,
    Callable,
    Literal,
    Protocol,
)

from cx_home import resolve_cx_home

CX_HOME = resolve_cx_home()
STAGE = CX_HOME / "runtime" / "cx2"

if str(STAGE) not in sys.path:
    sys.path.insert(
        0,
        str(STAGE),
    )

from client import (
    AppServerProtocolError,
)

from verification_gate import (
    CommandExecutionSummary,
    classify_command,
    extract_changed_files_from_diff,
    extract_changed_files_from_items,
    is_command_masked,
    unwrap_display_command,
)
from bounded_verification_runner import (
    is_verification_command_eligible,
    execute_bounded_verification_command,
)


# =============================================================
# Client protocol
#
# Works with both:
#
#   AppServerClient
#   synthetic test client
#
# =============================================================

class TurnClient(Protocol):

    def request(
        self,
        method: str,
        params: Any = ...,
        timeout: float = ...,
    ) -> Any:
        ...

    def respond(
        self,
        request_id: Any,
        result: Any,
    ) -> None:
        ...

    def respond_error(
        self,
        request_id: Any,
        code: int,
        message: str,
    ) -> None:
        ...

    def drain_notifications(
        self,
    ) -> list[dict[str, Any]]:
        ...

    def drain_matching_notifications(
        self,
        predicate: Callable[[dict[str, Any]], bool],
    ) -> list[dict[str, Any]]:
        ...

    def drain_server_requests(
        self,
    ) -> list[dict[str, Any]]:
        ...

    def drain_unknown(
        self,
    ) -> list[dict[str, Any]]:
        ...


FINAL_STATUSES = {
    "completed",
    "interrupted",
    "failed",
}

UNRESOLVED_ITEM_MAX_BYTES: int = 256 * 1024
UNRESOLVED_TURN_MAX_BYTES: int = 1024 * 1024
MAX_UNRESOLVED_PRESTART_ITEMS: int = 256
MAX_UNRESOLVED_ITEM_ID_BYTES: int = 256
MAX_DIAGNOSTIC_ITEM_ID_CHARS: int = 64
MAX_FINAL_ANSWER_CANDIDATES: int = 16
MAX_FINAL_CANDIDATE_ITEM_ID_BYTES: int = 256
FINAL_CANDIDATE_EVIDENCE_MAX_BYTES: int = 4 * 1024
MAX_FINAL_RECONCILIATION_RECORDS: int = 64
MAX_BOUNDED_COUNTER: int = (1 << 63) - 1

MAX_INTERACTIVE_APPROVAL_PROMPTS_PER_TURN: int = 6
TIMEOUT_RECONCILIATION_GRACE_SEC: float = 0.25


@dataclass
class TurnApprovalState:
    request_id_responses: dict[Any, dict[str, Any]] = field(default_factory=dict)
    declined_identities: set[tuple[str, str, str]] = field(default_factory=set)
    session_accepted_identities: set[tuple[str, str, str]] = field(default_factory=set)
    circuit_warning_rendered: bool = False


@dataclass
class AgentMessageItemState:
    """Authoritative, turn-scoped classification for one agent message item."""

    item_type: str | None = None
    phase: str | None = None
    lifecycle: str = "unresolved"
    buffered_text: str = ""
    buffered_bytes: int = 0
    dropped_bytes: int = 0
    overflow_event_count: int = 0
    unresolved_prestart: bool = False
    streamed_text: str = ""
    completed_text: str | None = None
    authoritative_digest: str | None = None
    authoritative_source: str | None = None
    canonical_final_candidate: bool = False
    started_summary_recorded: bool = False
    completed_summary_recorded: bool = False
    overflowed: bool = False


def _bounded_utf8_prefix(text: str, maximum_bytes: int) -> tuple[str, int]:
    if maximum_bytes <= 0 or not text:
        return "", 0

    encoded = text.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return text, len(encoded)

    bounded = encoded[:maximum_bytes]
    while bounded:
        try:
            value = bounded.decode("utf-8")
            return value, len(bounded)
        except UnicodeDecodeError as exc:
            bounded = bounded[:exc.start]

    return "", 0


def _append_bounded_utf8(existing: str, value: str, maximum_bytes: int) -> str:
    existing_prefix, existing_bytes = _bounded_utf8_prefix(
        existing,
        maximum_bytes,
    )
    if existing_bytes >= maximum_bytes:
        return existing_prefix
    suffix, _ = _bounded_utf8_prefix(
        value,
        maximum_bytes - existing_bytes,
    )
    return existing_prefix + suffix


def _increment_bounded_counter(value: int, amount: int = 1) -> int:
    return min(MAX_BOUNDED_COUNTER, max(0, int(value)) + max(0, int(amount)))


def _utf8_length_within(text: str, maximum_bytes: int) -> int | None:
    """Return the UTF-8 length, stopping as soon as a fixed limit is exceeded."""

    total = 0
    for character in text:
        total += len(character.encode("utf-8"))
        if total > maximum_bytes:
            return None
    return total


def _bounded_diagnostic_identifier(value: str) -> str:
    if len(value) <= MAX_DIAGNOSTIC_ITEM_ID_CHARS:
        return value

    digest = hashlib.sha256()
    chunk_chars = 1024
    for offset in range(0, len(value), chunk_chars):
        digest.update(value[offset : offset + chunk_chars].encode("utf-8"))
    prefix = value[: MAX_DIAGNOSTIC_ITEM_ID_CHARS - 17]
    return f"{prefix}...#{digest.hexdigest()[:12]}"


class TurnTimeoutError(TimeoutError):
    """Typed, bounded diagnostic for an idle or hard turn timeout."""

    def __init__(
        self,
        *,
        kind: Literal["idle", "hard"],
        turn_id: str,
        elapsed_seconds: float,
        idle_seconds: float,
        configured_idle_timeout: float,
        configured_hard_timeout: float,
        interrupt_requested: bool,
        last_meaningful_activity_category: str | None,
        result: "TurnRunResult",
    ) -> None:
        self.kind = kind
        self.turn_id = turn_id
        self.elapsed_seconds = max(0.0, float(elapsed_seconds))
        self.idle_seconds = max(0.0, float(idle_seconds))
        self.configured_idle_timeout = float(configured_idle_timeout)
        self.configured_hard_timeout = float(configured_hard_timeout)
        self.interrupt_requested = bool(interrupt_requested)
        self.last_meaningful_activity_category = (
            last_meaningful_activity_category
        )
        self.result = result
        super().__init__(f"turn {kind} timeout: {turn_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_kind": self.kind,
            "turn_id": self.turn_id,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "idle_seconds": round(self.idle_seconds, 3),
            "configured_idle_timeout_sec": self.configured_idle_timeout,
            "configured_hard_timeout_sec": self.configured_hard_timeout,
            "whether_interrupt_requested": self.interrupt_requested,
            "last_meaningful_activity_category": (
                self.last_meaningful_activity_category
            ),
        }


# =============================================================
# Structured result
# =============================================================

@dataclass
class TurnRunResult:

    thread_id: str
    turn_id: str

    status: str = "inProgress"

    error: dict[str, Any] | None = None

    agent_text: str = ""

    agent_message_items: dict[
        tuple[str, str, str],
        AgentMessageItemState,
    ] = field(default_factory=dict)
    canonical_final_item_id: str | None = None
    canonical_final_source: str | None = None
    confirmed_streamed_final: bool = False
    authoritative_final_evidence: bool = False
    canonical_final_reconciled: bool = False
    final_candidate_count: int = 0
    final_candidates_retained: int = 0
    final_candidates_dropped: int = 0
    final_candidate_overflow_events: int = 0
    final_candidate_rejection_diagnostic: str | None = None
    duplicate_final_count: int = 0
    final_ambiguity_reason: str | None = None
    protocol_failure_reason: str | None = None
    raw_final_seen: bool = False
    raw_final_text: str | None = None
    raw_final_digest: str | None = None
    raw_final_duplicate_count: int = 0
    raw_final_conflict: bool = False
    raw_final_conflict_text: str | None = None
    raw_final_conflict_digest: str | None = None
    unresolved_agent_bytes: int = 0
    unresolved_prestart_items: int = 0
    unresolved_items_dropped: int = 0
    unresolved_bytes_dropped: int = 0
    overflow_event_count: int = 0
    unresolved_overflow_warning_emitted: bool = False
    identity_rejection_count: int = 0
    identity_rejections: dict[str, int] = field(default_factory=dict)
    final_reconciliations: list[dict[str, Any]] = field(default_factory=list)
    final_reconciliation_events_dropped: int = 0

    latest_diff: str = ""

    token_usage: dict[str, Any] | None = None

    command_output: dict[
        str,
        str,
    ] = field(
        default_factory=dict
    )

    command_accumulators: dict[
        str,
        Any,
    ] = field(
        default_factory=dict
    )

    started_items: list[
        dict[str, Any]
    ] = field(
        default_factory=list
    )

    completed_items: list[
        dict[str, Any]
    ] = field(
        default_factory=list
    )

    warnings: list[
        str
    ] = field(
        default_factory=list
    )

    errors: list[
        dict[str, Any]
    ] = field(
        default_factory=list
    )

    server_request_actions: list[
        dict[str, Any]
    ] = field(
        default_factory=list
    )

    unknown_messages: list[
        dict[str, Any]
    ] = field(
        default_factory=list
    )

    reasoning_event_count: int = 0
    reasoning_delta_chars: int = 0

    interrupt_requested: bool = False

    timeout_diagnostics: dict[str, Any] | None = None
    last_meaningful_activity_category: str | None = None

    started_at: float | None = None
    completed_at: float | None = None

    duration_ms: int | None = None

    final_turn: dict[str, Any] | None = None

    changed_files: list[str] = field(
        default_factory=list
    )

    command_executions: list[dict[str, Any]] = field(
        default_factory=list
    )

    event_sequence: int = 0
    last_mutation_sequence: int = 0

    # Phase 4: Approval state & telemetry
    server_approval_request_count: int = 0
    interactive_approval_prompt_count: int = 0
    exact_replay_count: int = 0
    auto_decline_count: int = 0
    human_approval_wait_seconds: float = 0.0
    circuit_breaker_opened: bool = False

    approval_state: TurnApprovalState = field(
        default_factory=lambda: TurnApprovalState()
    )

    @property
    def outcome(self) -> str:
        timeout_kind = (
            self.timeout_diagnostics.get("timeout_kind")
            if isinstance(self.timeout_diagnostics, dict)
            else None
        )
        if timeout_kind == "idle":
            return "IDLE_TIMEOUT"
        if timeout_kind == "hard":
            return "HARD_TIMEOUT"

        normalized = str(self.status).casefold()
        if normalized in {"completed", "success"}:
            return "COMPLETED"
        if normalized == "blocked":
            return "BLOCKED"
        if normalized == "interrupted" or self.interrupt_requested:
            return "INTERRUPTED"
        if normalized == "failed":
            return "FAILED"
        return "PROCESS_OR_PROTOCOL_FAILURE"

    def to_dict(
        self,
    ) -> dict[str, Any]:

        return asdict(
            self
        )


# =============================================================
# Safe item projection
#
# Never persist raw reasoning item contents.
# =============================================================


# CX2_AGENT_COMPLETION_FALLBACK_V1
def safe_agent_message_text(
    item: Any,
) -> str | None:
    """
    Extract displayable final text from an App Server agentMessage item.

    This is a completion fallback only. Raw reasoning is never accepted
    here and no arbitrary tool payload is rendered.
    """

    if not isinstance(
        item,
        dict,
    ):
        return None

    if item.get(
        "type"
    ) != "agentMessage":
        return None

    # Current/generated contracts may expose a simple string.
    for key in (
        "text",
        "message",
    ):
        value = item.get(
            key
        )

        if isinstance(
            value,
            str,
        ):
            return value

    # Be tolerant of structured message content.
    content = item.get(
        "content"
    )

    if isinstance(
        content,
        str,
    ):
        return content

    if not isinstance(
        content,
        list,
    ):
        return None

    parts: list[str] = []
    output_text_seen = False

    for entry in content:

        if isinstance(
            entry,
            str,
        ):
            output_text_seen = True
            if entry:
                parts.append(
                    entry
                )

            continue

        if not isinstance(
            entry,
            dict,
        ):
            continue

        entry_type = str(
            entry.get(
                "type",
                "",
            )
        )

        # Explicitly reject anything reasoning-like.
        if "reason" in entry_type.casefold():
            continue

        value = entry.get(
            "text"
        )

        if isinstance(
            value,
            str,
        ):
            output_text_seen = True
            if value:
                parts.append(
                    value
                )

    if not output_text_seen:
        return None

    return "".join(
        parts
    )


MAX_COMMAND_OUTPUT_BYTES_RETAINED = 512 * 1024
MAX_HEAD_BYTES = 64 * 1024
MAX_TAIL_BYTES = MAX_COMMAND_OUTPUT_BYTES_RETAINED - MAX_HEAD_BYTES


class BoundedDiagnosticAccumulator:
    """
    Deterministic Head + Tail streaming diagnostic accumulator.

    Guarantees:
      1. Memory usage per command is strictly bounded <= MAX_COMMAND_OUTPUT_BYTES_RETAINED + small buffer.
      2. If total stream bytes <= MAX_COMMAND_OUTPUT_BYTES_RETAINED, retains the full stream verbatim.
      3. If total stream bytes > MAX_COMMAND_OUTPUT_BYTES_RETAINED:
         - Retains the first 64 KiB in head (preserves initial command context / startup logs).
         - Retains the most recent 448 KiB in rolling tail (preserves late errors / diagnostics).
         - Assembles as: head + f"\n... [truncated {truncated_bytes} bytes] ...\n" + tail
      4. Multibyte UTF-8 integrity is maintained across chunk boundaries.
    """

    def __init__(
        self,
        max_total_bytes: int = MAX_COMMAND_OUTPUT_BYTES_RETAINED,
        max_head_bytes: int = MAX_HEAD_BYTES,
    ) -> None:
        self.max_total_bytes = max_total_bytes
        self.max_head_bytes = min(max_head_bytes, max_total_bytes)
        self.max_tail_bytes = max_total_bytes - self.max_head_bytes

        self.total_bytes_streamed: int = 0
        self._head_bytes = bytearray()
        self._tail_bytes = bytearray()
        self._is_split = False

    def push(self, delta: str | bytes) -> None:
        if not delta:
            return
        raw_bytes = (
            delta.encode("utf-8", errors="replace")
            if isinstance(delta, str)
            else delta
        )
        delta_len = len(raw_bytes)
        self.total_bytes_streamed += delta_len

        if not self._is_split:
            if len(self._head_bytes) + delta_len <= self.max_total_bytes:
                self._head_bytes.extend(raw_bytes)
                return
            else:
                self._is_split = True
                combined = self._head_bytes + raw_bytes
                self._head_bytes = combined[:self.max_head_bytes]
                tail_part = combined[self.max_head_bytes:]
                if len(tail_part) > self.max_tail_bytes:
                    self._tail_bytes = tail_part[-self.max_tail_bytes:]
                else:
                    self._tail_bytes = bytearray(tail_part)
                return

        self._tail_bytes.extend(raw_bytes)
        if len(self._tail_bytes) > self.max_tail_bytes:
            self._tail_bytes = self._tail_bytes[-self.max_tail_bytes:]

    def get_diagnostic_text(self) -> str:
        if not self._is_split:
            return self._head_bytes.decode("utf-8", errors="replace")

        head_str = self._head_bytes.decode("utf-8", errors="replace")
        tail_str = self._tail_bytes.decode("utf-8", errors="replace")
        truncated_bytes = max(
            0,
            self.total_bytes_streamed
            - len(self._head_bytes)
            - len(self._tail_bytes),
        )

        if truncated_bytes > 0:
            sep = f"\n... [truncated {truncated_bytes} bytes] ...\n"
            return head_str + sep + tail_str
        return head_str + tail_str


def extract_bounded_window_text(
    raw_text: str,
    max_total_bytes: int = MAX_COMMAND_OUTPUT_BYTES_RETAINED,
    max_head_bytes: int = MAX_HEAD_BYTES,
) -> str:
    """
    Extract a deterministic Head + Tail window from static text if it exceeds max_total_bytes.
    """
    if not raw_text:
        return ""
    raw_bytes = raw_text.encode("utf-8", errors="replace")
    if len(raw_bytes) <= max_total_bytes:
        return raw_text

    max_head = min(max_head_bytes, max_total_bytes)
    max_tail = max_total_bytes - max_head

    head_bytes = raw_bytes[:max_head]
    tail_bytes = raw_bytes[-max_tail:]
    truncated_bytes = len(raw_bytes) - max_head - max_tail

    head_str = head_bytes.decode("utf-8", errors="replace")
    tail_str = tail_bytes.decode("utf-8", errors="replace")
    sep = f"\n... [truncated {truncated_bytes} bytes] ...\n"
    return head_str + sep + tail_str


def extract_command_diagnostic_text(
    item: Any,
    accumulated_stream: Any = "",
    max_bytes: int = MAX_COMMAND_OUTPUT_BYTES_RETAINED,
) -> str:
    """
    Deterministically extract diagnostic text for a command execution.

    Precedence:
      1. Non-empty inline fields on completed item:
         - aggregatedOutput (standard Codex App Server completion payload)
         - output
         - error
         - stderr
      2. Bounded accumulated outputDelta stream for the same item ID

    Head + Tail bounded window is applied to preserve early context and late diagnostics
    while strictly capping memory retention.
    """
    raw = ""
    if isinstance(item, dict):
        raw = (
            item.get("aggregatedOutput")
            or item.get("output")
            or item.get("error")
            or item.get("stderr")
            or ""
        )
        if not isinstance(raw, str):
            raw = str(raw) if raw is not None else ""

    if raw.strip():
        return extract_bounded_window_text(raw, max_total_bytes=max_bytes)

    if isinstance(accumulated_stream, BoundedDiagnosticAccumulator):
        return accumulated_stream.get_diagnostic_text()
    elif isinstance(accumulated_stream, str) and accumulated_stream.strip():
        return extract_bounded_window_text(accumulated_stream, max_total_bytes=max_bytes)

    return ""


def safe_item_summary(
    item: Any,
    accumulated_stream: Any = "",
) -> dict[str, Any]:

    if not isinstance(
        item,
        dict,
    ):
        return {
            "type": "unknown",
        }

    result: dict[
        str,
        Any,
    ] = {}

    for key in (
        "id",
        "type",
        "phase",
        "status",
        "command",
        "cwd",
        "processId",
        "exitCode",
        "durationMs",
    ):

        if key in item:
            result[
                key
            ] = item[
                key
            ]

    # Bounded output snippet for error / status diagnosis
    if result.get("type") == "commandExecution":
        diagnostic_text = extract_command_diagnostic_text(
            item,
            accumulated_stream=accumulated_stream,
        )
        if diagnostic_text.strip():
            result["output_snippet"] = diagnostic_text.strip()[:500]

    # Do not include raw reasoning content.
    if (
        result.get(
            "type"
        )
        == "reasoning"
    ):
        return result

    # Agent message fallback only when generated contract
    # exposes a simple string field.
    if (
        result.get(
            "type"
        )
        == "agentMessage"
    ):

        for key in (
            "text",
            "message",
        ):
            value = item.get(
                key
            )

            if isinstance(
                value,
                str,
            ):
                result[
                    key
                ] = value

    # Keep only change count for file-change items.
    if (
        result.get(
            "type"
        )
        == "fileChange"
    ):

        changes = item.get(
            "changes"
        )

        if isinstance(
            changes,
            list,
        ):
            result[
                "changeCount"
            ] = len(
                changes
            )

    return result


# =============================================================
# Streaming runtime
# =============================================================


# CX2_STDIO_SAFETY_V1
def _configure_live_stdio_safety() -> None:
    """
    Prevent Windows legacy console/code-page encoders from
    crashing a Codex turn when streamed text contains characters
    outside the active code page.

    Keep the current encoding. Only change error handling to
    replacement mode, so PowerShell/console compatibility remains
    unchanged while unsupported glyphs degrade safely to '?'.
    """

    for stream in (
        sys.stdout,
        sys.stderr,
    ):

        reconfigure = getattr(
            stream,
            "reconfigure",
            None,
        )

        if not callable(
            reconfigure
        ):
            continue

        try:
            reconfigure(
                errors="replace"
            )
        except (
            ValueError,
            OSError,
            AttributeError,
        ):
            pass


# CX2_RICH_TERMINAL_V1
from terminal_ui import TerminalRenderer


_CX2_TERMINAL = TerminalRenderer()



# CX2_THREAD_READ_FINAL_FALLBACK_V1
def _cx2_agent_final_candidates(
    items: Any,
) -> list[tuple[str, str]]:
    """Return exact-ID, explicitly phased final-answer items, including empty text."""

    if not isinstance(items, list):
        return []

    candidates: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            continue
        if item.get("type") != "agentMessage":
            continue
        if item.get("phase") != "final_answer":
            continue
        value = item.get("text")
        if isinstance(value, str):
            candidates.append((item_id, value))
    return candidates


def _cx2_extract_thread_final_answer(
    payload: Any,
    *,
    expected_turn_id: str,
    expected_thread_id: str | None = None,
) -> str | None:
    """
    Extract only the final agent answer for the exact completed turn.

    Accepted shape is intentionally narrow:
      thread.turns[].id == expected_turn_id
      item.type == agentMessage
      item.phase == final_answer
      item.text is str

    Reasoning/tool/user payloads are never accepted.
    """

    if not isinstance(
        payload,
        dict,
    ):
        return None

    thread = payload.get(
        "thread"
    )

    if not isinstance(
        thread,
        dict,
    ):
        return None

    if (
        expected_thread_id is not None
        and thread.get("id") != expected_thread_id
    ):
        return None

    turns = thread.get(
        "turns"
    )

    if not isinstance(
        turns,
        list,
    ):
        return None

    for turn in turns:

        if not isinstance(
            turn,
            dict,
        ):
            continue

        if str(
            turn.get(
                "id",
                "",
            )
        ) != str(
            expected_turn_id
        ):
            continue

        candidates = _cx2_agent_final_candidates(turn.get("items"))
        return candidates[-1][1] if candidates else None

    return None


def _cx2_extract_thread_final_candidates(
    payload: Any,
    *,
    expected_thread_id: str,
    expected_turn_id: str,
) -> list[tuple[str, str]]:
    if not isinstance(payload, dict):
        return []
    thread = payload.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != expected_thread_id:
        return []
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        if turn.get("id") != expected_turn_id:
            continue
        return _cx2_agent_final_candidates(turn.get("items"))
    return []



# CX2_TURN_COMPLETED_DIRECT_FINAL_V1

# CX2_RAW_RESPONSE_FINAL_V1
def _cx2_extract_raw_response_final_answer(
    item: Any,
) -> str | None:
    """
    Extract only an assistant final_answer from a completed raw
    ResponseItem.

    This intentionally ignores:
      - user/developer messages
      - commentary
      - reasoning
      - tool calls / outputs
      - encrypted content
      - arbitrary response-item variants
    """

    if not isinstance(
        item,
        dict,
    ):
        return None

    if item.get(
        "type"
    ) != "message":
        return None

    if item.get(
        "role"
    ) != "assistant":
        return None

    if item.get(
        "phase"
    ) != "final_answer":
        return None

    content = item.get(
        "content"
    )

    if not isinstance(
        content,
        list,
    ):
        return None

    parts: list[str] = []
    output_text_seen = False

    for entry in content:

        if not isinstance(
            entry,
            dict,
        ):
            continue

        if entry.get(
            "type"
        ) != "output_text":
            continue

        output_text_seen = True

        value = entry.get(
            "text"
        )

        if isinstance(
            value,
            str,
        ):
            parts.append(
                value
            )

    if not output_text_seen:
        return None

    return "".join(
        parts
    )


def _cx2_extract_turn_final_answer(
    turn: Any,
) -> str | None:
    """
    Extract only a final-answer agentMessage from a completed Turn.

    Accepted structure:
      turn.items[]
      item.type == "agentMessage"
      item.phase == "final_answer"
      item.text is str (including an explicitly empty final answer)

    User/tool/reasoning/commentary items are never rendered.
    """

    if not isinstance(
        turn,
        dict,
    ):
        return None

    items = turn.get(
        "items"
    )

    if not isinstance(
        items,
        list,
    ):
        return None

    candidates = _cx2_agent_final_candidates(items)
    return candidates[-1][1] if candidates else None


# CX2_STRUCTURED_USER_INPUT_V1
def build_turn_input(
    prompt: str,
    input_items: list[dict] | None = None,
) -> list[dict]:

    result: list[dict] = [
        {
            "type":
                "text",

            "text":
                prompt,

            "text_elements":
                [],
        }
    ]

    if input_items is None:
        return result

    if not isinstance(
        input_items,
        list,
    ):
        raise ValueError(
            "CX2 input_items must be a list."
        )

    for raw_item in input_items:

        if not isinstance(
            raw_item,
            dict,
        ):
            raise ValueError(
                "CX2 input item must be an object."
            )

        item = dict(
            raw_item
        )

        kind = item.get(
            "type"
        )

        if kind == "image":

            url = item.get(
                "url"
            )

            if not isinstance(
                url,
                str,
            ) or not url:
                raise ValueError(
                    "CX2 image input requires url."
                )

            allowed = {
                "type",
                "url",
                "detail",
            }

        elif kind == "localImage":

            path = item.get(
                "path"
            )

            if not isinstance(
                path,
                str,
            ) or not path:
                raise ValueError(
                    "CX2 localImage input requires path."
                )

            allowed = {
                "type",
                "path",
                "detail",
            }

        elif kind == "mention":

            name = item.get(
                "name"
            )

            path = item.get(
                "path"
            )

            if (
                not isinstance(
                    name,
                    str,
                )
                or not name
                or not isinstance(
                    path,
                    str,
                )
                or not path
            ):
                raise ValueError(
                    "CX2 mention input requires name + path."
                )

            allowed = {
                "type",
                "name",
                "path",
            }

        else:
            raise ValueError(
                "Unsupported CX2 structured input type: "
                + repr(
                    kind
                )
            )

        unexpected = (
            set(
                item
            )
            - allowed
        )

        if unexpected:
            raise ValueError(
                "Unexpected fields for CX2 input "
                f"{kind}: {sorted(unexpected)}"
            )

        detail = item.get(
            "detail"
        )

        if (
            detail is not None
            and detail not in {
                "auto",
                "low",
                "high",
                "original",
            }
        ):
            raise ValueError(
                "Unsupported image detail: "
                + repr(
                    detail
                )
            )

        result.append(
            item
        )

    return result


class StreamingTurnRunner:

    def __init__(
        self,
        client: TurnClient,
        *,
        live: bool = True,
        poll_interval: float = 0.03,
        max_approval_prompts_per_turn: int = MAX_INTERACTIVE_APPROVAL_PROMPTS_PER_TURN,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        timeout_reconciliation_grace: float = TIMEOUT_RECONCILIATION_GRACE_SEC,
    ) -> None:

        self.client = client
        self.live = live

        if self.live:
            _configure_live_stdio_safety()

        self.poll_interval = max(
            0.001,
            float(
                poll_interval
            ),
        )
        self.max_approval_prompts_per_turn = max(
            1,
            int(max_approval_prompts_per_turn),
        )
        self._monotonic = monotonic or (lambda: time.monotonic())
        self._sleep = sleeper or (lambda seconds: time.sleep(seconds))
        self.timeout_reconciliation_grace = max(
            0.0,
            min(float(timeout_reconciliation_grace), 5.0),
        )

    @staticmethod
    def _agent_item_key(
        result: TurnRunResult,
        item_id: str,
    ) -> tuple[str, str, str]:
        return (result.thread_id, result.turn_id, item_id)

    def _agent_item_state(
        self,
        result: TurnRunResult,
        item_id: str,
    ) -> AgentMessageItemState:
        key = self._agent_item_key(result, item_id)
        state = result.agent_message_items.get(key)
        if state is None:
            state = AgentMessageItemState()
            result.agent_message_items[key] = state
        return state

    @staticmethod
    def _diagnostic_item_id(item_id: str | None) -> str | None:
        if item_id is None:
            return None
        return _bounded_diagnostic_identifier(item_id)

    @staticmethod
    def _bound_final_candidate_summary(
        summary: dict[str, Any],
    ) -> None:
        for key in ("text", "message"):
            summary.pop(key, None)

    @staticmethod
    def _source_rank(source: str | None) -> int:
        return {
            "item/completed": 1,
            "rawResponseItem/completed": 2,
            "turn/start": 3,
            "turn/completed": 3,
            "thread/read": 4,
        }.get(source or "", 99)

    @staticmethod
    def _record_reconciliation(
        result: TurnRunResult,
        *,
        item_id: str | None,
        source: str,
        streamed: str,
        authoritative: str,
    ) -> tuple[str, dict[str, Any]]:
        if streamed == authoritative:
            relationship = "identical"
        elif not streamed:
            relationship = "missing_streamed"
        elif authoritative.startswith(streamed):
            relationship = "streamed_prefix"
        elif streamed.startswith(authoritative):
            relationship = "completed_prefix"
        else:
            relationship = "divergent"

        record = {
            "item_id": StreamingTurnRunner._diagnostic_item_id(item_id),
            "source": source,
            "relationship": relationship,
            "streamed_bytes": len(streamed.encode("utf-8")),
            "authoritative_bytes": len(authoritative.encode("utf-8")),
        }
        if len(result.final_reconciliations) < MAX_FINAL_RECONCILIATION_RECORDS:
            result.final_reconciliations.append(record)
        else:
            result.final_reconciliation_events_dropped = (
                _increment_bounded_counter(
                    result.final_reconciliation_events_dropped
                )
            )
        return relationship, record

    def _render_canonical_delta(self, delta: str) -> None:
        if self.live and delta:
            _CX2_TERMINAL.agent_delta(delta)

    @staticmethod
    def _discard_agent_item_state(
        result: TurnRunResult,
        key: tuple[str, str, str],
    ) -> None:
        state = result.agent_message_items.pop(key, None)
        if state is None:
            return
        if state.unresolved_prestart:
            result.unresolved_prestart_items = max(
                0,
                result.unresolved_prestart_items - 1,
            )
        if state.buffered_bytes:
            result.unresolved_agent_bytes = max(
                0,
                result.unresolved_agent_bytes - state.buffered_bytes,
            )

    def _mark_final_protocol_failure(
        self,
        result: TurnRunResult,
        *,
        reason: str,
        evidence: str,
        record: dict[str, Any] | None = None,
    ) -> None:
        if record is not None:
            record["selected"] = False
            record["reason"] = reason
        result.authoritative_final_evidence = True
        result.canonical_final_reconciled = False
        result.final_ambiguity_reason = reason
        result.protocol_failure_reason = reason
        result.agent_text = ""
        result.canonical_final_item_id = None
        result.canonical_final_source = None

        for state in result.agent_message_items.values():
            if state.authoritative_digest is None:
                continue
            if state.completed_text is not None:
                state.completed_text = _bounded_utf8_prefix(
                    state.completed_text,
                    FINAL_CANDIDATE_EVIDENCE_MAX_BYTES,
                )[0]
            state.streamed_text = _bounded_utf8_prefix(
                state.streamed_text,
                FINAL_CANDIDATE_EVIDENCE_MAX_BYTES,
            )[0]

        if evidence not in result.warnings:
            result.warnings.append(evidence)
            if self.live:
                _CX2_TERMINAL.response_ambiguity(reason)

    def _reject_final_candidate(
        self,
        result: TurnRunResult,
        *,
        item_id: str,
        reason: str,
        evidence: str,
    ) -> None:
        result.final_candidates_dropped = _increment_bounded_counter(
            result.final_candidates_dropped
        )
        result.final_candidate_overflow_events = _increment_bounded_counter(
            result.final_candidate_overflow_events
        )
        if result.final_candidate_rejection_diagnostic is None:
            result.final_candidate_rejection_diagnostic = (
                _bounded_diagnostic_identifier(item_id)
            )
        self._mark_final_protocol_failure(
            result,
            reason=reason,
            evidence=evidence,
        )

    def _reserve_final_candidate_state(
        self,
        result: TurnRunResult,
        item_id: str,
    ) -> AgentMessageItemState | None:
        if (
            _utf8_length_within(
                item_id,
                MAX_FINAL_CANDIDATE_ITEM_ID_BYTES,
            )
            is None
        ):
            self._reject_final_candidate(
                result,
                item_id=item_id,
                reason="FINAL_ANSWER_CANDIDATE_ID_INVALID",
                evidence=(
                    "A final-answer candidate item ID exceeded the retained "
                    "identity bound and was rejected."
                ),
            )
            return None

        key = self._agent_item_key(result, item_id)
        state = result.agent_message_items.get(key)
        if state is not None and state.canonical_final_candidate:
            return state

        if result.final_candidates_retained >= MAX_FINAL_ANSWER_CANDIDATES:
            if state is not None:
                self._discard_agent_item_state(result, key)
            self._reject_final_candidate(
                result,
                item_id=item_id,
                reason="FINAL_ANSWER_CANDIDATE_LIMIT_EXCEEDED",
                evidence=(
                    "The per-turn final-answer candidate limit was exceeded; "
                    "additional candidate identities were discarded."
                ),
            )
            return None

        if state is None:
            state = AgentMessageItemState()
            result.agent_message_items[key] = state
        state.canonical_final_candidate = True
        state.item_type = "agentMessage"
        state.phase = "final_answer"
        result.final_candidates_retained += 1
        result.final_candidate_count = result.final_candidates_retained
        return state

    def _mark_final_ambiguity(
        self,
        result: TurnRunResult,
        record: dict[str, Any],
    ) -> None:
        reason = "MULTIPLE_FINAL_ANSWER_AMBIGUOUS"
        self._mark_final_protocol_failure(
            result,
            reason=reason,
            evidence=(
                "Multiple distinct final-answer item IDs supplied different "
                "authoritative text; canonical success was rejected."
            ),
            record=record,
        )

    def _set_authoritative_final(
        self,
        result: TurnRunResult,
        *,
        text: str,
        source: str,
        item_id: str | None = None,
    ) -> None:
        streamed = result.agent_text

        state: AgentMessageItemState | None = None
        if item_id is not None:
            state = self._reserve_final_candidate_state(
                result,
                item_id,
            )
            if state is None:
                return

        relationship, record = self._record_reconciliation(
            result,
            item_id=item_id,
            source=source,
            streamed=streamed,
            authoritative=text,
        )

        incoming_rank = self._source_rank(source)

        if item_id is not None and state is not None:
            if state.unresolved_prestart:
                state.unresolved_prestart = False
                result.unresolved_prestart_items = max(
                    0,
                    result.unresolved_prestart_items - 1,
                )
            if state.buffered_bytes:
                result.unresolved_agent_bytes = max(
                    0,
                    result.unresolved_agent_bytes - state.buffered_bytes,
                )
                state.buffered_text = ""
                state.buffered_bytes = 0

            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            current_item_rank = self._source_rank(state.authoritative_source)

            if state.authoritative_digest is not None:
                if incoming_rank > current_item_rank:
                    record["selected"] = False
                    record["reason"] = "lower_precedence_same_item"
                    return
                if (
                    incoming_rank == current_item_rank
                    and state.authoritative_digest != digest
                ):
                    self._mark_final_ambiguity(result, record)
                    return
            state.authoritative_digest = digest
            state.authoritative_source = source
            state.completed_text = _bounded_utf8_prefix(
                text,
                FINAL_CANDIDATE_EVIDENCE_MAX_BYTES,
            )[0]
            # Completed authoritative evidence supersedes the streamed
            # candidate copy, keeping total retained text at 4 KiB per ID.
            state.streamed_text = ""

            candidate_states = [
                (key[2], candidate)
                for key, candidate in result.agent_message_items.items()
                if candidate.authoritative_digest is not None
            ]
            candidate_digests = {
                candidate.authoritative_digest
                for _, candidate in candidate_states
            }
            if len(candidate_digests) > 1:
                self._mark_final_ambiguity(result, record)
                return
            if len(candidate_states) > 1:
                result.duplicate_final_count = len(candidate_states) - 1
                record["selected"] = False
                record["duplicate_equivalent"] = True
                result.authoritative_final_evidence = True
                result.canonical_final_reconciled = True
                result.canonical_final_item_id = min(
                    candidate_id for candidate_id, _ in candidate_states
                )
                return

        if result.final_ambiguity_reason is not None:
            record["selected"] = False
            record["reason"] = result.final_ambiguity_reason
            return

        current_rank = self._source_rank(result.canonical_final_source)
        if result.canonical_final_source and incoming_rank > current_rank:
            record["selected"] = False
            record["reason"] = "lower_precedence_source"
            return

        record["selected"] = True
        result.authoritative_final_evidence = True
        result.canonical_final_reconciled = True

        if self.live:
            if relationship in {"streamed_prefix", "missing_streamed"}:
                self._render_canonical_delta(text[len(streamed):])
            elif relationship in {"completed_prefix", "divergent"}:
                _CX2_TERMINAL.response_reconciled(text)
            elif relationship == "identical" and not text:
                _CX2_TERMINAL.confirm_empty_response()

        result.agent_text = text
        result.canonical_final_source = source
        if item_id:
            result.canonical_final_item_id = item_id

    def _set_raw_authoritative_final(
        self,
        result: TurnRunResult,
        text: str,
    ) -> None:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        bounded_text = _bounded_utf8_prefix(
            text,
            FINAL_CANDIDATE_EVIDENCE_MAX_BYTES,
        )[0]

        if not result.raw_final_seen:
            result.raw_final_seen = True
            result.raw_final_text = bounded_text
            result.raw_final_digest = digest
            self._set_authoritative_final(
                result,
                text=text,
                source="rawResponseItem/completed",
            )
            return

        relationship, record = self._record_reconciliation(
            result,
            item_id=None,
            source="rawResponseItem/completed",
            streamed=result.agent_text,
            authoritative=text,
        )

        known_digests = {
            value
            for value in (
                result.raw_final_digest,
                result.raw_final_conflict_digest,
            )
            if value is not None
        }
        if digest in known_digests:
            result.raw_final_duplicate_count = _increment_bounded_counter(
                result.raw_final_duplicate_count
            )
            record["selected"] = False
            record["duplicate_equivalent"] = True
            record["relationship"] = relationship
            return

        if not result.raw_final_conflict:
            evidence_pair = sorted(
                (
                    (result.raw_final_digest or "", result.raw_final_text or ""),
                    (digest, bounded_text),
                ),
                key=lambda value: value[0],
            )
            result.raw_final_digest = evidence_pair[0][0]
            result.raw_final_text = evidence_pair[0][1]
            result.raw_final_conflict_digest = evidence_pair[1][0]
            result.raw_final_conflict_text = evidence_pair[1][1]
        result.raw_final_conflict = True
        self._mark_final_protocol_failure(
            result,
            reason="MULTIPLE_RAW_FINAL_ANSWER_AMBIGUOUS",
            evidence=(
                "Multiple raw final-answer events supplied different "
                "authoritative text; canonical success was rejected."
            ),
            record=record,
        )

    def _classify_agent_item(
        self,
        result: TurnRunResult,
        item: dict[str, Any],
        *,
        lifecycle: str,
    ) -> AgentMessageItemState | None:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            return None

        item_type = item.get("type")
        phase = item.get("phase")
        is_final_candidate = (
            item_type == "agentMessage"
            and phase == "final_answer"
        )
        if is_final_candidate:
            state = self._reserve_final_candidate_state(
                result,
                item_id,
            )
            if state is None:
                return None
        else:
            state = self._agent_item_state(result, item_id)

        if state.unresolved_prestart:
            state.unresolved_prestart = False
            result.unresolved_prestart_items = max(
                0,
                result.unresolved_prestart_items - 1,
            )
        state.item_type = item_type if isinstance(item_type, str) else None
        state.phase = phase if isinstance(phase, str) else None
        state.lifecycle = lifecycle
        state.canonical_final_candidate = (
            state.item_type == "agentMessage"
            and state.phase == "final_answer"
        )

        classification_resolved = (
            state.canonical_final_candidate
            or (
                state.item_type == "agentMessage"
                and state.phase == "commentary"
            )
            or (
                state.item_type is not None
                and state.item_type != "agentMessage"
            )
        )

        if classification_resolved and state.buffered_bytes:
            result.unresolved_agent_bytes = max(
                0,
                result.unresolved_agent_bytes - state.buffered_bytes,
            )

        if state.canonical_final_candidate and state.buffered_text:
            state.streamed_text = _append_bounded_utf8(
                state.streamed_text,
                state.buffered_text,
                FINAL_CANDIDATE_EVIDENCE_MAX_BYTES,
            )
            result.confirmed_streamed_final = True
            if result.canonical_final_item_id in {None, item_id}:
                result.canonical_final_item_id = item_id
                result.agent_text += state.buffered_text
                self._render_canonical_delta(state.buffered_text)

        if classification_resolved:
            state.buffered_text = ""
            state.buffered_bytes = 0
        return state

    def _drop_unresolved_delta(
        self,
        result: TurnRunResult,
        delta_bytes: int,
        *,
        dropped_item: bool,
    ) -> None:
        if dropped_item:
            result.unresolved_items_dropped += 1
        result.unresolved_bytes_dropped += delta_bytes
        result.overflow_event_count += 1
        if not result.unresolved_overflow_warning_emitted:
            result.unresolved_overflow_warning_emitted = True
            result.warnings.append(
                "Unresolved agentMessage candidate overflow bounds were exceeded; "
                "additional pre-start text was discarded from canonical consideration."
            )

    def _buffer_unresolved_delta(
        self,
        result: TurnRunResult,
        state: AgentMessageItemState,
        delta: str,
        *,
        item_id: str,
    ) -> None:
        item_remaining = UNRESOLVED_ITEM_MAX_BYTES - state.buffered_bytes
        turn_remaining = UNRESOLVED_TURN_MAX_BYTES - result.unresolved_agent_bytes
        accepted, accepted_bytes = _bounded_utf8_prefix(
            delta,
            min(item_remaining, turn_remaining),
        )
        if accepted:
            state.buffered_text += accepted
            state.buffered_bytes += accepted_bytes
            result.unresolved_agent_bytes += accepted_bytes

        if accepted_bytes < len(delta.encode("utf-8")):
            dropped_bytes = len(delta.encode("utf-8")) - accepted_bytes
            state.overflowed = True
            state.dropped_bytes += dropped_bytes
            state.overflow_event_count += 1
            self._drop_unresolved_delta(
                result,
                dropped_bytes,
                dropped_item=False,
            )

    def _handle_agent_delta(
        self,
        result: TurnRunResult,
        params: dict[str, Any],
    ) -> None:
        if params.get("threadId") != result.thread_id:
            return
        if params.get("turnId") != result.turn_id:
            return

        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not item_id:
            return
        if not isinstance(delta, str) or not delta:
            return

        key = self._agent_item_key(result, item_id)
        state = result.agent_message_items.get(key)
        if state is None:
            delta_bytes = len(delta.encode("utf-8"))
            if result.final_ambiguity_reason in {
                "FINAL_ANSWER_CANDIDATE_LIMIT_EXCEEDED",
                "FINAL_ANSWER_CANDIDATE_ID_INVALID",
            }:
                self._drop_unresolved_delta(
                    result,
                    delta_bytes,
                    dropped_item=True,
                )
                return
            item_id_bytes = len(item_id.encode("utf-8"))
            if (
                item_id_bytes > MAX_UNRESOLVED_ITEM_ID_BYTES
                or result.unresolved_prestart_items
                >= MAX_UNRESOLVED_PRESTART_ITEMS
            ):
                self._drop_unresolved_delta(
                    result,
                    delta_bytes,
                    dropped_item=True,
                )
                return
            state = AgentMessageItemState(unresolved_prestart=True)
            result.agent_message_items[key] = state
            result.unresolved_prestart_items += 1

        if state.canonical_final_candidate:
            state.streamed_text = _append_bounded_utf8(
                state.streamed_text,
                delta,
                FINAL_CANDIDATE_EVIDENCE_MAX_BYTES,
            )
            result.confirmed_streamed_final = True
            if result.canonical_final_item_id in {None, item_id}:
                result.canonical_final_item_id = item_id
                result.agent_text += delta
                self._render_canonical_delta(delta)
            return

        if state.item_type == "agentMessage" and state.phase == "commentary":
            return

        self._buffer_unresolved_delta(
            result,
            state,
            delta,
            item_id=item_id,
        )

    # ---------------------------------------------------------
    # turn/start
    # ---------------------------------------------------------

    def run_turn(
        self,
        *,
        thread_id: str,
        prompt: str,
        cwd: Path,
        model: str,
        effort: str,
        permissions: str,
        approval_policy: str,
        input_items: list[dict] | None = None,
        timeout: float | None = 300.0,
        idle_timeout: float | None = None,
        hard_timeout: float | None = None,
    ) -> TurnRunResult:

        if self.live:
            _CX2_TERMINAL.begin_turn()

        # CX2_TURN_APPROVAL_POLICY_ARG_V1
        if approval_policy not in {
            "never",
            "on-request",
        }:
            raise ValueError(
                "Unsupported CX2 approval policy: "
                + repr(approval_policy)
            )

        self.current_permissions = permissions
        self.current_cwd = cwd

        params = {
            "threadId":
                thread_id,

            "input":
                build_turn_input(
                    prompt,
                    input_items,
                ),

            "cwd":
                str(
                    cwd.resolve()
                ),

            "runtimeWorkspaceRoots": [
                str(
                    cwd.resolve()
                ),
            ],

            "approvalPolicy":
                approval_policy,

            "permissions":
                permissions,

            "model":
                model,

            "effort":
                effort,
        }

        response = self.client.request(
            "turn/start",
            params,
            timeout=30.0,
        )

        if not isinstance(
            response,
            dict,
        ):
            raise RuntimeError(
                "turn/start result object değil."
            )

        turn = response.get(
            "turn"
        )

        if not isinstance(
            turn,
            dict,
        ):
            raise RuntimeError(
                "turn/start result.turn yok."
            )

        turn_id = turn.get(
            "id"
        )

        if not isinstance(
            turn_id,
            str,
        ) or not turn_id:
            raise RuntimeError(
                "turn/start turn.id yok."
            )

        status = str(
            turn.get(
                "status",
                "inProgress",
            )
        )

        result = TurnRunResult(
            thread_id=
                thread_id,

            turn_id=
                turn_id,

            status=
                status,

            error=
                turn.get(
                    "error"
                )
                if isinstance(
                    turn.get(
                        "error"
                    ),
                    dict,
                )
                else None,

            started_at=
                turn.get(
                    "startedAt"
                ),

            completed_at=
                turn.get(
                    "completedAt"
                ),

            duration_ms=
                turn.get(
                    "durationMs"
                ),

            final_turn=
                turn
                if status
                in FINAL_STATUSES
                else None,
        )

        if status in FINAL_STATUSES:
            if status == "completed":
                for final_item_id, direct_final_text in (
                    _cx2_agent_final_candidates(turn.get("items"))
                ):
                    self._set_authoritative_final(
                        result,
                        text=direct_final_text,
                        source="turn/start",
                        item_id=final_item_id,
                    )

            self._finalize_terminal_result(result)

            if self.live:
                _CX2_TERMINAL.turn_completed(
                    result.status,
                    duration_ms=result.duration_ms,
                    line_count=self._canonical_line_count(result.agent_text),
                )

            return result

        try:
            completed_result = self.wait_for_turn(
                result,
                timeout=timeout,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
            )
        except KeyboardInterrupt:
            if result.status not in FINAL_STATUSES:
                result.status = "interrupted"
            self._finalize_terminal_result(result, allow_recovery=False)
            if self.live:
                _CX2_TERMINAL.turn_completed(result.outcome.casefold())
            raise
        except Exception:
            if result.status not in FINAL_STATUSES:
                result.status = "failed"
            self._finalize_terminal_result(result, allow_recovery=False)
            if self.live:
                _CX2_TERMINAL.turn_completed(result.outcome.casefold())
            raise

        self._finalize_terminal_result(completed_result)

        if self.live:
            _CX2_TERMINAL.turn_completed(
                completed_result.status,
                duration_ms=completed_result.duration_ms,
                line_count=self._canonical_line_count(completed_result.agent_text),
            )

        return completed_result

    @staticmethod
    def _canonical_line_count(text: str) -> int:
        if not text:
            return 0
        return text.count("\n") + (0 if text.endswith("\n") else 1)

    # ---------------------------------------------------------
    # Event loop
    # ---------------------------------------------------------

    def wait_for_turn(
        self,
        result: TurnRunResult,
        *,
        timeout: float | None = None,
        idle_timeout: float | None = None,
        hard_timeout: float | None = None,
    ) -> TurnRunResult:
        # ``timeout`` is retained as a direct-call compatibility alias. The
        # production runtime supplies both explicit limits.
        legacy_timeout = 300.0 if timeout is None else float(timeout)
        resolved_idle_timeout = max(
            0.001,
            float(
                legacy_timeout
                if idle_timeout is None
                else idle_timeout
            ),
        )
        resolved_hard_timeout = max(
            resolved_idle_timeout,
            float(
                legacy_timeout
                if hard_timeout is None
                else hard_timeout
            ),
        )

        turn_start_monotonic = self._monotonic()
        last_meaningful_activity_monotonic = turn_start_monotonic
        last_meaningful_activity_category = "turn/start-ack"
        result.last_meaningful_activity_category = (
            last_meaningful_activity_category
        )
        active_work_items: set[str] = set()
        interrupted_once = False
        user_interrupt_deadline: float | None = None

        while True:

            try:
                activity_categories = self._drain_turn_events(
                    result,
                    active_work_items,
                )

                if activity_categories:
                    last_meaningful_activity_monotonic = self._monotonic()
                    last_meaningful_activity_category = activity_categories[-1]
                    result.last_meaningful_activity_category = (
                        last_meaningful_activity_category
                    )

                if (
                    result.status
                    in FINAL_STATUSES
                ):
                    return result

                if self._reconcile_transport_liveness(
                    result,
                    active_work_items,
                ):
                    return result

                now = self._monotonic()
                active_elapsed = max(
                    0.0,
                    now
                    - turn_start_monotonic
                    - result.human_approval_wait_seconds,
                )
                idle_elapsed = max(
                    0.0,
                    now - last_meaningful_activity_monotonic,
                )

                timeout_kind: Literal["idle", "hard"] | None = None
                if active_elapsed >= resolved_hard_timeout:
                    timeout_kind = "hard"
                elif (
                    not active_work_items
                    and idle_elapsed >= resolved_idle_timeout
                ):
                    timeout_kind = "idle"

                if timeout_kind is not None:
                    # One last drain before interruption. Terminal completion wins;
                    # progress delivered on an idle boundary starts a new idle window.
                    boundary_activity = self._drain_turn_events(
                        result,
                        active_work_items,
                    )
                    if result.status in FINAL_STATUSES:
                        return result
                    if boundary_activity and timeout_kind == "idle":
                        last_meaningful_activity_monotonic = self._monotonic()
                        last_meaningful_activity_category = boundary_activity[-1]
                        result.last_meaningful_activity_category = (
                            last_meaningful_activity_category
                        )
                        continue

                    interrupt_requested = self._request_interrupt_once(result)
                    reconciliation_now = self._monotonic()
                    reconciliation_deadline = (
                        reconciliation_now
                        + self.timeout_reconciliation_grace
                    )
                    while reconciliation_now < reconciliation_deadline:
                        remaining = reconciliation_deadline - reconciliation_now
                        self._sleep(min(self.poll_interval, remaining))
                        self._drain_turn_events(result, active_work_items)
                        if self._reconcile_transport_liveness(
                            result,
                            active_work_items,
                        ):
                            break
                        next_reconciliation_now = self._monotonic()
                        # A controlled/test clock that does not advance must not
                        # turn bounded reconciliation into an infinite wait.
                        if next_reconciliation_now <= reconciliation_now:
                            break
                        reconciliation_now = next_reconciliation_now

                    final_now = reconciliation_now
                    final_active_elapsed = max(
                        0.0,
                        final_now
                        - turn_start_monotonic
                        - result.human_approval_wait_seconds,
                    )
                    final_idle_elapsed = max(
                        0.0,
                        final_now - last_meaningful_activity_monotonic,
                    )
                    if result.status not in FINAL_STATUSES:
                        result.status = "failed"
                    error = TurnTimeoutError(
                        kind=timeout_kind,
                        turn_id=result.turn_id,
                        elapsed_seconds=final_active_elapsed,
                        idle_seconds=final_idle_elapsed,
                        configured_idle_timeout=resolved_idle_timeout,
                        configured_hard_timeout=resolved_hard_timeout,
                        interrupt_requested=interrupt_requested,
                        last_meaningful_activity_category=(
                            last_meaningful_activity_category
                        ),
                        result=result,
                    )
                    result.timeout_diagnostics = error.to_dict()
                    raise error

                if (
                    user_interrupt_deadline is not None
                    and now >= user_interrupt_deadline
                ):
                    if result.status not in FINAL_STATUSES:
                        result.status = "interrupted"
                    raise KeyboardInterrupt

                self._sleep(self.poll_interval)

            except KeyboardInterrupt:

                if interrupted_once:
                    if result.status not in FINAL_STATUSES:
                        result.status = "interrupted"
                    raise

                interrupted_once = True

                if self.live:
                    _CX2_TERMINAL.interrupting()

                self._request_interrupt_once(result)
                last_meaningful_activity_monotonic = self._monotonic()
                last_meaningful_activity_category = "user_interrupt"
                result.last_meaningful_activity_category = (
                    last_meaningful_activity_category
                )
                user_interrupt_deadline = self._monotonic() + 15.0

    @staticmethod
    def _event_matches_turn(
        result: TurnRunResult,
        params: dict[str, Any],
    ) -> bool:
        event_thread = params.get("threadId")
        event_turn = params.get("turnId")
        nested_turn = params.get("turn")
        if event_turn is None and isinstance(nested_turn, dict):
            event_turn = nested_turn.get("id")
        if (
            isinstance(event_thread, str)
            and event_thread
            and event_thread != result.thread_id
        ):
            return False
        if (
            isinstance(event_turn, str)
            and event_turn
            and event_turn != result.turn_id
        ):
            return False
        return True

    def _notification_activity(
        self,
        result: TurnRunResult,
        notification: dict[str, Any],
        active_work_items: set[str],
    ) -> str | None:
        method = notification.get("method")
        params = notification.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return None
        if (
            self._requires_exact_canonical_identity(method, params)
            and not self._has_exact_event_identity(result, params)
        ):
            return None
        if (
            method == "turn/completed"
            and not self._has_exact_turn_completed_identity(result, params)
        ):
            return None
        if not self._event_matches_turn(result, params):
            return None

        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if not isinstance(item, dict):
                return None
            item_id = item.get("id")
            item_type = item.get("type")
            if method == "item/started" and item_type == "commandExecution":
                if isinstance(item_id, str) and item_id:
                    active_work_items.add(item_id)
            elif method == "item/completed":
                if isinstance(item_id, str) and item_id:
                    active_work_items.discard(item_id)
            return method

        if method in {
            "item/agentMessage/delta",
            "item/commandExecution/outputDelta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
        }:
            if method == "item/agentMessage/delta":
                if params.get("threadId") != result.thread_id:
                    return None
                if params.get("turnId") != result.turn_id:
                    return None
                item_id = params.get("itemId")
                if not isinstance(item_id, str) or not item_id:
                    return None
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                return method
            return None

        if method == "turn/diff/updated":
            diff = params.get("diff")
            return method if isinstance(diff, str) and diff else None

        if method == "rawResponseItem/completed":
            return method if params.get("item") is not None else None

        if method == "turn/started":
            return method

        if method in {"warning", "error"}:
            # Warning/error chatter only counts when explicitly turn-scoped.
            if params.get("turnId") == result.turn_id:
                return method

        # Token telemetry and unknown protocol noise deliberately do not count.
        return None

    def _server_request_activity(
        self,
        result: TurnRunResult,
        request: dict[str, Any],
    ) -> str | None:
        method = request.get("method")
        params = request.get("params")
        if not isinstance(params, dict):
            params = {}
        if not self._event_matches_turn(result, params):
            return None
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
        }:
            return "server_request"
        return None

    def _drain_turn_events(
        self,
        result: TurnRunResult,
        active_work_items: set[str],
    ) -> list[str]:
        activities: list[str] = []
        # Server requests are handled first because approval may pause the turn.
        for request in self.client.drain_server_requests():
            activity = self._server_request_activity(result, request)
            self._handle_server_request(result, request)
            if activity is not None:
                activities.append(activity)

        for notification in self.client.drain_notifications():
            activity = self._notification_activity(
                result,
                notification,
                active_work_items,
            )
            self._handle_notification(result, notification)
            if activity is not None:
                activities.append(activity)

        unknown = self.client.drain_unknown()
        if unknown:
            result.unknown_messages.extend(unknown)
        return activities

    def _is_matching_late_final_notification(
        self,
        result: TurnRunResult,
        notification: dict[str, Any],
    ) -> bool:
        method = notification.get("method")
        params = notification.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return False
        if method == "turn/completed":
            return self._has_exact_turn_completed_identity(result, params)
        if method not in {
            "item/started",
            "item/completed",
            "item/agentMessage/delta",
            "rawResponseItem/completed",
        }:
            return False
        if not self._has_exact_event_identity(result, params):
            return False
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            return isinstance(item, dict) and item.get("type") == "agentMessage"
        return True

    def _drain_late_final_events(
        self,
        result: TurnRunResult,
    ) -> None:
        """Bound the terminal boundary while accepting only exact-turn final evidence."""

        start = self._monotonic()
        deadline = start + self.timeout_reconciliation_grace
        now = start
        selective_drain = getattr(
            self.client,
            "drain_matching_notifications",
            None,
        )
        if not callable(selective_drain):
            # A transport that cannot preserve unmatched notifications must not
            # participate in terminal-boundary draining.
            return
        while True:
            for notification in selective_drain(
                lambda value: self._is_matching_late_final_notification(
                    result,
                    value,
                )
            ):
                self._handle_notification(result, notification)

            if now >= deadline:
                return
            remaining = deadline - now
            self._sleep(min(self.poll_interval, remaining))
            next_now = self._monotonic()
            if next_now <= now:
                return
            now = next_now

    def _finalize_terminal_result(
        self,
        result: TurnRunResult,
        *,
        allow_recovery: bool = True,
    ) -> None:
        observed_status = str(result.status)
        observed_error = result.error
        self._drain_late_final_events(result)

        if observed_status.casefold() not in {"completed", "success"}:
            result.status = observed_status
            result.error = observed_error
            return

        if str(result.status).casefold() not in {"completed", "success"}:
            return

        if (
            allow_recovery
            and not result.authoritative_final_evidence
            and result.final_ambiguity_reason is None
        ):
            self._recover_final_answer_from_thread(result)

        if (
            result.final_ambiguity_reason is not None
            or not result.authoritative_final_evidence
            or not result.canonical_final_reconciled
        ):
            reason = (
                result.final_ambiguity_reason
                or result.protocol_failure_reason
                or "MISSING_AUTHORITATIVE_FINAL"
            )
            result.protocol_failure_reason = reason
            result.status = "failed"
            if result.error is None:
                result.error = {
                    "reason": reason,
                    "message": (
                        "Completed turn did not yield one strictly reconciled "
                        "authoritative final answer."
                    ),
                }
            evidence = (
                "Completed turn rejected canonical success because strict "
                f"final reconciliation failed ({reason})."
            )
            if evidence not in result.warnings:
                result.warnings.append(evidence)

    def _request_interrupt_once(self, result: TurnRunResult) -> bool:
        if result.interrupt_requested:
            return True
        result.interrupt_requested = True
        try:
            self.interrupt(result.thread_id, result.turn_id)
        except Exception:
            pass
        return True

    def _reconcile_transport_liveness(
        self,
        result: TurnRunResult,
        active_work_items: set[str],
    ) -> bool:
        process = getattr(self.client, "process", None)
        dispatcher = getattr(self.client, "_dispatcher_thread", None)
        poll_code = None
        if process is not None:
            try:
                poll_code = process.poll()
            except Exception:
                poll_code = None
        dispatcher_alive = (
            getattr(dispatcher, "is_alive", lambda: True)()
            if dispatcher is not None
            else True
        )
        stopped = (
            poll_code is not None
            or (dispatcher is not None and not dispatcher_alive)
        )
        if not stopped:
            return False

        if (
            dispatcher is not None
            and getattr(dispatcher, "is_alive", lambda: False)()
        ):
            try:
                dispatcher.join(timeout=1.0)
            except Exception:
                pass

        dispatcher_alive_after_join = (
            getattr(dispatcher, "is_alive", lambda: False)()
            if dispatcher is not None
            else False
        )
        self._drain_turn_events(result, active_work_items)
        if result.status in FINAL_STATUSES:
            return True

        result.status = "failed"
        if poll_code is not None:
            if dispatcher_alive_after_join:
                raise AppServerProtocolError(
                    "Codex App Server process terminated unexpectedly and dispatcher "
                    "failed to quiesce within 1.0s grace "
                    f"(exit code: {poll_code}, turn: {result.turn_id})."
                )
            raise AppServerProtocolError(
                "Codex App Server process terminated unexpectedly "
                f"(exit code: {poll_code}, turn: {result.turn_id})."
            )
        raise AppServerProtocolError(
            "Codex App Server dispatcher thread terminated unexpectedly "
            f"(turn: {result.turn_id})."
        )

    # ---------------------------------------------------------
    # Ctrl+C / explicit interrupt
    # ---------------------------------------------------------

    def interrupt(
        self,
        thread_id: str,
        turn_id: str,
    ) -> Any:

        return self.client.request(
            "turn/interrupt",
            {
                "threadId":
                    thread_id,

                "turnId":
                    turn_id,
            },
            timeout=15.0,
        )

    # ---------------------------------------------------------
    # Safe interactive approval prompt
    # ---------------------------------------------------------

    def _safe_approval_prompt(
        self,
        result: TurnRunResult,
        *,
        title: str,
        details: list[str],
        decisions: list[str],
        default: str,
    ) -> str:
        deny_like = (
            "decline",
            "denied",
            "cancel",
            "abort",
        )

        safe_default = (
            default
            if default in decisions
            else ""
        )

        if not safe_default:
            for candidate in deny_like:
                if candidate in decisions:
                    safe_default = candidate
                    break

        if not safe_default:
            return ""

        if not self.live or not getattr(_CX2_TERMINAL, "can_prompt", False):
            return safe_default

        # Measure human blocking time
        result.interactive_approval_prompt_count += 1
        t_start = self._monotonic()
        try:
            decision = _CX2_TERMINAL.approval_prompt(
                title=title,
                details=details,
                decisions=decisions,
                default_decision=safe_default,
            )
        finally:
            t_end = self._monotonic()
            wait_sec = max(0.0, t_end - t_start)
            result.human_approval_wait_seconds += wait_sec

        if decision not in decisions:
            return safe_default

        return decision

    # ---------------------------------------------------------
    # CX2 2.0.10 Bounded verification execution offer
    # ---------------------------------------------------------

    def _handle_bounded_verification_offer(
        self,
        result: TurnRunResult,
        *,
        cmd_str: str,
        disp_cmd: str,
        cwd: str,
        raw_record: dict[str, Any],
    ) -> None:
        cmd_identity = (
            "bounded_verification_exec",
            str(cwd).strip(),
            str(disp_cmd or cmd_str).strip(),
        )

        # 1. Check circuit-breaker
        if result.interactive_approval_prompt_count >= self.max_approval_prompts_per_turn:
            result.circuit_breaker_opened = True
            if not result.approval_state.circuit_warning_rendered:
                if self.live:
                    _CX2_TERMINAL.warning(
                        f"Tur içi onay sınırı aşıldı ({result.interactive_approval_prompt_count}/{self.max_approval_prompts_per_turn}). Kalan onay istekleri otomatik reddediliyor."
                    )
                result.approval_state.circuit_warning_rendered = True
            result.auto_decline_count += 1
            return

        # 2. Check previously declined identities
        if cmd_identity in result.approval_state.declined_identities:
            result.auto_decline_count += 1
            return

        # 3. Prompt user with transparent warning
        details = [
            f"Command: {disp_cmd or cmd_str}",
            f"CWD: {cwd}",
            "This command will execute outside the read-only sandbox and may modify files.",
        ]

        decision = self._safe_approval_prompt(
            result,
            title="Verification command requires writable runtime access",
            details=details,
            decisions=["accept", "decline"],
            default="decline",
        )

        if decision == "accept":
            bounded_res = execute_bounded_verification_command(
                command=disp_cmd or cmd_str,
                cwd=cwd,
                timeout=60.0,
            )
            raw_record["exit_code"] = bounded_res.exit_code
            raw_record["output_snippet"] = bounded_res.output_snippet
            raw_record["classification_text"] = bounded_res.classification_text
            raw_record["duration_ms"] = (raw_record.get("duration_ms") or 0) + bounded_res.duration_ms
            raw_record["bounded_host_execution"] = True

            result.command_output[cmd_str] = bounded_res.classification_text
            result.server_request_actions.append({
                "method": "bounded_verification_exec",
                "action": f"accept:{bounded_res.exit_code}",
            })
        else:
            result.approval_state.declined_identities.add(cmd_identity)
            result.server_request_actions.append({
                "method": "bounded_verification_exec",
                "action": "decline",
            })

    # ---------------------------------------------------------
    # Server -> client requests
    # ---------------------------------------------------------

    # CX2_INTERACTIVE_APPROVAL_DISPATCH_V1
    def _handle_server_request(
        self,
        result: TurnRunResult,
        request: dict[str, Any],
    ) -> None:

        request_id = request.get(
            "id"
        )

        method = request.get(
            "method"
        )

        params = request.get(
            "params"
        )

        if not isinstance(
            params,
            dict,
        ):
            params = {}

        if not isinstance(
            method,
            str,
        ):
            result.server_request_actions.append(
                {
                    "method":
                        None,

                    "action":
                        "error",
                }
            )

            self.client.respond_error(
                request_id,
                -32600,
                "Invalid server request.",
            )

            return

        result.server_approval_request_count += 1
        approval_state = result.approval_state

        def record(
            action: str,
        ) -> None:

            result.server_request_actions.append(
                {
                    "method":
                        method,

                    "action":
                        action,
                }
            )

        # -----------------------------------------------------
        # Case A: Same request ID replay idempotency
        # -----------------------------------------------------
        if request_id is not None and request_id in approval_state.request_id_responses:
            cached_response = approval_state.request_id_responses[request_id]
            result.exact_replay_count += 1
            self.client.respond(
                request_id,
                cached_response,
            )
            decision = cached_response.get("decision", "cached")
            record(f"replay:{decision}")
            return

        def safe_prompt(
            *,
            title: str,
            details: list[str],
            decisions: list[str],
            default: str,
        ) -> str:
            return self._safe_approval_prompt(
                result,
                title=title,
                details=details,
                decisions=decisions,
                default=default,
            )

        def send_decision(
            decision: str,
            identity: tuple[str, str, str] | None = None,
        ) -> None:
            payload = {"decision": decision}
            self.client.respond(
                request_id,
                payload,
            )
            if request_id is not None:
                approval_state.request_id_responses[request_id] = payload
            if identity is not None:
                if decision in ("decline", "denied", "cancel", "abort"):
                    approval_state.declined_identities.add(identity)
                elif decision in ("acceptForSession", "approved_for_session"):
                    approval_state.session_accepted_identities.add(identity)
            record(decision)

        # -----------------------------------------------------
        # Modern command approval
        # -----------------------------------------------------

        if (
            method
            == "item/commandExecution/requestApproval"
        ):

            known = {
                "accept",
                "acceptForSession",
                "decline",
                "cancel",
            }

            raw_available = params.get(
                "availableDecisions"
            )

            if isinstance(
                raw_available,
                list,
            ):
                decisions = [
                    value
                    for value in raw_available
                    if (
                        isinstance(
                            value,
                            str,
                        )
                        and value in known
                    )
                ]
            else:
                decisions = [
                    "accept",
                    "acceptForSession",
                    "decline",
                    "cancel",
                ]

            details: list[str] = []

            reason = params.get(
                "reason"
            )

            command = params.get(
                "command"
            )

            cwd = params.get(
                "cwd"
            )

            if isinstance(
                reason,
                str,
            ) and reason:
                details.append(
                    "Reason: "
                    + reason
                )

            if isinstance(
                cwd,
                str,
            ) and cwd:
                details.append(
                    "CWD: "
                    + cwd
                )

            if isinstance(
                command,
                str,
            ) and command:
                details.append(
                    "Command: "
                    + command
                )

            if params.get(
                "additionalPermissions"
            ) is not None:
                details.append(
                    "Additional permissions requested."
                )

            # Build exact authorization identity
            cmd_text = str(command or "").strip()
            cwd_text = str(cwd or "").strip()
            identity = (method, cwd_text, cmd_text)

            # Check explicit session accept memory
            if identity in approval_state.session_accepted_identities:
                session_decision = "acceptForSession" if "acceptForSession" in decisions else ("accept" if "accept" in decisions else "decline")
                send_decision(session_decision, identity=identity)
                return

            # Check exact decline memory
            if identity in approval_state.declined_identities:
                result.auto_decline_count += 1
                decline_decision = "decline" if "decline" in decisions else "cancel"
                send_decision(decline_decision, identity=identity)
                return

            # Check circuit breaker
            if result.interactive_approval_prompt_count >= self.max_approval_prompts_per_turn:
                result.circuit_breaker_opened = True
                if not approval_state.circuit_warning_rendered:
                    if self.live:
                        _CX2_TERMINAL.warning(
                            f"Tur içi onay sınırı aşıldı ({result.interactive_approval_prompt_count}/{self.max_approval_prompts_per_turn}). Kalan komut onay istekleri otomatik reddediliyor."
                        )
                    approval_state.circuit_warning_rendered = True
                decline_decision = "decline" if "decline" in decisions else "cancel"
                send_decision(decline_decision, identity=identity)
                return

            decision = safe_prompt(
                title="Command execution",
                details=details,
                decisions=decisions,
                default="decline",
            )

            if not decision:
                self.client.respond_error(
                    request_id,
                    -32000,
                    (
                        "No safe string decision available "
                        "for command approval."
                    ),
                )

                record(
                    "deny-error"
                )

                return

            send_decision(decision, identity=identity)
            return

        # -----------------------------------------------------
        # Modern file-change approval
        # -----------------------------------------------------

        if (
            method
            == "item/fileChange/requestApproval"
        ):

            if (
                self.live
                and result.latest_diff
            ):
                _CX2_TERMINAL.diff_updated(
                    result.latest_diff
                )

            details = []

            reason = params.get(
                "reason"
            )

            grant_root = params.get(
                "grantRoot"
            )

            if isinstance(
                reason,
                str,
            ) and reason:
                details.append(
                    "Reason: "
                    + reason
                )

            if isinstance(
                grant_root,
                str,
            ) and grant_root:
                details.append(
                    "Requested root: "
                    + grant_root
                )

            decisions = [
                "accept",
                "acceptForSession",
                "decline",
                "cancel",
            ]

            # Check circuit breaker for file changes too
            if result.interactive_approval_prompt_count >= self.max_approval_prompts_per_turn:
                result.circuit_breaker_opened = True
                if not approval_state.circuit_warning_rendered:
                    if self.live:
                        _CX2_TERMINAL.warning(
                            f"Tur içi onay sınırı aşıldı ({result.interactive_approval_prompt_count}/{self.max_approval_prompts_per_turn}). Kalan onay istekleri otomatik reddediliyor."
                        )
                    approval_state.circuit_warning_rendered = True
                send_decision("decline")
                return

            decision = safe_prompt(
                title="File changes",
                details=details,
                decisions=decisions,
                default="decline",
            )

            send_decision(decision)
            return

        # -----------------------------------------------------
        # Legacy command approval
        # -----------------------------------------------------

        if method == "execCommandApproval":

            details = []

            reason = params.get(
                "reason"
            )

            cwd = params.get(
                "cwd"
            )

            command = params.get(
                "command"
            )

            if isinstance(
                reason,
                str,
            ) and reason:
                details.append(
                    "Reason: "
                    + reason
                )

            if isinstance(
                cwd,
                str,
            ) and cwd:
                details.append(
                    "CWD: "
                    + cwd
                )

            command_text = ""
            if isinstance(
                command,
                list,
            ):
                command_text = " ".join(
                    str(part)
                    for part in command
                ).strip()

                if command_text:
                    details.append(
                        "Command: "
                        + command_text
                    )
            elif isinstance(command, str):
                command_text = command.strip()
                if command_text:
                    details.append(
                        "Command: "
                        + command_text
                    )

            decisions = [
                "approved",
                "approved_for_session",
                "denied",
                "abort",
            ]

            cwd_text = str(cwd or "").strip()
            identity = (method, cwd_text, command_text)

            if identity in approval_state.session_accepted_identities:
                session_decision = "approved_for_session" if "approved_for_session" in decisions else "approved"
                send_decision(session_decision, identity=identity)
                return

            if identity in approval_state.declined_identities:
                result.auto_decline_count += 1
                decline_decision = "denied" if "denied" in decisions else "abort"
                send_decision(decline_decision, identity=identity)
                return

            if result.interactive_approval_prompt_count >= self.max_approval_prompts_per_turn:
                result.circuit_breaker_opened = True
                if not approval_state.circuit_warning_rendered:
                    if self.live:
                        _CX2_TERMINAL.warning(
                            f"Tur içi onay sınırı aşıldı ({result.interactive_approval_prompt_count}/{self.max_approval_prompts_per_turn}). Kalan komut onay istekleri otomatik reddediliyor."
                        )
                    approval_state.circuit_warning_rendered = True
                decline_decision = "denied" if "denied" in decisions else "abort"
                send_decision(decline_decision, identity=identity)
                return

            decision = safe_prompt(
                title="Command execution",
                details=details,
                decisions=decisions,
                default="denied",
            )

            if not decision:
                self.client.respond_error(
                    request_id,
                    -32000,
                    (
                        "No safe string decision available "
                        "for command approval."
                    ),
                )

                record(
                    "deny-error"
                )

                return

            send_decision(decision, identity=identity)
            return

        # -----------------------------------------------------
        # Legacy apply-patch approval
        # -----------------------------------------------------

        if method == "applyPatchApproval":

            if (
                self.live
                and result.latest_diff
            ):
                _CX2_TERMINAL.diff_updated(
                    result.latest_diff
                )

            details = []

            reason = params.get(
                "reason"
            )

            grant_root = params.get(
                "grantRoot"
            )

            file_changes = params.get(
                "fileChanges"
            )

            if isinstance(
                reason,
                str,
            ) and reason:
                details.append(
                    "Reason: "
                    + reason
                )

            if isinstance(
                grant_root,
                str,
            ) and grant_root:
                details.append(
                    "Requested root: "
                    + grant_root
                )

            if isinstance(
                file_changes,
                dict,
            ) and file_changes:
                names = sorted(
                    str(name)
                    for name in file_changes.keys()
                )

                details.append(
                    "Files: "
                    + ", ".join(
                        names[:8]
                    )
                )

            decisions = [
                "approved",
                "approved_for_session",
                "denied",
                "abort",
            ]

            if result.interactive_approval_prompt_count >= self.max_approval_prompts_per_turn:
                result.circuit_breaker_opened = True
                if not approval_state.circuit_warning_rendered:
                    if self.live:
                        _CX2_TERMINAL.warning(
                            f"Tur içi onay sınırı aşıldı ({result.interactive_approval_prompt_count}/{self.max_approval_prompts_per_turn}). Kalan onay istekleri otomatik reddediliyor."
                        )
                    approval_state.circuit_warning_rendered = True
                send_decision("denied")
                return

            decision = safe_prompt(
                title="File changes",
                details=details,
                decisions=decisions,
                default="denied",
            )

            send_decision(decision)
            return

        # -----------------------------------------------------
        # request_user_input remains fail-safe for this phase.
        # A dedicated interactive question UX belongs to the
        # later input-surface phase.
        # -----------------------------------------------------

        if (
            method
            == "item/tool/requestUserInput"
        ):

            self.client.respond(
                request_id,
                {
                    "answers":
                        {},
                },
            )

            record(
                "empty-answer"
            )

            return

        # -----------------------------------------------------
        # Permission escalation:
        #
        # The generated response has no deny decision. Never
        # manufacture a GrantedPermissionProfile.
        # -----------------------------------------------------

        if (
            method
            == "item/permissions/requestApproval"
        ):

            self.client.respond_error(
                request_id,
                -32000,
                "Denied by CX2 safe permission policy.",
            )

            record(
                "deny-error"
            )

            return

        # -----------------------------------------------------
        # Unknown server requests must always be resolved.
        # -----------------------------------------------------

        self.client.respond_error(
            request_id,
            -32601,
            (
                "Unsupported server request "
                f"in CX2: {method}"
            ),
        )

        record(
            "unsupported-error"
        )


    # ---------------------------------------------------------
    # Server notifications
    # ---------------------------------------------------------

    @staticmethod
    def _has_exact_event_identity(
        result: TurnRunResult,
        params: dict[str, Any],
    ) -> bool:
        return (
            params.get("threadId") == result.thread_id
            and params.get("turnId") == result.turn_id
        )

    @staticmethod
    def _requires_exact_canonical_identity(
        method: str,
        params: dict[str, Any],
    ) -> bool:
        if method in {
            "item/agentMessage/delta",
            "rawResponseItem/completed",
        }:
            return True
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            return isinstance(item, dict) and item.get("type") == "agentMessage"
        return False

    @staticmethod
    def _has_exact_turn_completed_identity(
        result: TurnRunResult,
        params: dict[str, Any],
    ) -> bool:
        turn = params.get("turn")
        if not isinstance(turn, dict):
            return False
        if params.get("threadId") != result.thread_id:
            return False
        if turn.get("id") != result.turn_id:
            return False
        if "turnId" in params and params.get("turnId") != result.turn_id:
            return False
        return True

    @staticmethod
    def _record_identity_rejection(
        result: TurnRunResult,
        method: str,
    ) -> None:
        result.identity_rejection_count += 1
        result.identity_rejections[method] = (
            result.identity_rejections.get(method, 0) + 1
        )
        evidence = (
            "Rejected canonical-output event with missing, malformed, or "
            "mismatched turn identity."
        )
        if evidence not in result.warnings:
            result.warnings.append(evidence)

    # CX2_POST_WAIT_FINAL_RECOVERY_V1
    def _recover_final_answer_from_thread(
        self,
        result: TurnRunResult,
        *,
        attempts: int = 10,
        retry_delay: float = 0.15,
    ) -> str | None:
        """
        Zero-inference final-answer recovery after turn completion.

        App Server may emit turn/completed slightly before the persisted
        thread view exposes the final agentMessage. Therefore recovery
        is performed after wait_for_turn() returns and uses a short,
        bounded retry window.

        Confirmed streamed text is deliberately not treated as authoritative
        completion evidence.
        """

        attempt_count = max(
            1,
            int(attempts),
        )

        delay = max(
            0.0,
            float(retry_delay),
        )

        last_error: Exception | None = None

        for attempt_index in range(
            attempt_count
        ):

            try:
                response = self.client.request(
                    "thread/read",
                    {
                        "threadId":
                            result.thread_id,

                        "includeTurns":
                            True,
                    },
                    timeout=5.0,
                )

                last_error = None

            except Exception as exc:
                response = None
                last_error = exc

            candidates = _cx2_extract_thread_final_candidates(
                response,
                expected_thread_id=result.thread_id,
                expected_turn_id=result.turn_id,
            )

            if candidates:
                for item_id, value in candidates:
                    self._set_authoritative_final(
                        result,
                        text=value,
                        source="thread/read",
                        item_id=item_id,
                    )
                return result.agent_text

            if (
                attempt_index
                + 1
                < attempt_count
                and delay > 0.0
            ):
                self._sleep(
                    delay
                )

        if last_error is not None:
            result.warnings.append(
                "Final answer thread/read fallback failed after "
                + str(attempt_count)
                + " attempts: "
                + repr(last_error)[:300]
            )

        return None


    def _handle_notification(
        self,
        result: TurnRunResult,
        notification: dict[str, Any],
    ) -> None:

        method = notification.get(
            "method"
        )

        params = notification.get(
            "params"
        )

        if not isinstance(
            method,
            str,
        ):
            return

        if not isinstance(
            params,
            dict,
        ):
            params = {}

        if self._requires_exact_canonical_identity(method, params):
            if not self._has_exact_event_identity(result, params):
                self._record_identity_rejection(result, method)
                return
        elif method == "turn/completed":
            if not self._has_exact_turn_completed_identity(result, params):
                self._record_identity_rejection(result, method)
                return

        # Ignore unrelated thread/turn notifications.
        event_thread = params.get(
            "threadId"
        )

        event_turn = params.get(
            "turnId"
        )

        if (
            isinstance(
                event_thread,
                str,
            )
            and event_thread
            != result.thread_id
        ):
            return

        if (
            isinstance(
                event_turn,
                str,
            )
            and event_turn
            != result.turn_id
        ):
            return

        # ---------------------------------------------
        # CX2_NATIVE_WEB_EVENT_DISPATCH_V1
        # Native App Server ThreadItem:
        #   {"type": "webSearch", ...}
        # Lifecycle:
        #   item/started -> item/completed
        # ---------------------------------------------

        if method in {
            "item/started",
            "item/completed",
        }:

            item = params.get(
                "item"
            )

            if (
                isinstance(
                    item,
                    dict,
                )
                and item.get(
                    "type"
                )
                == "webSearch"
            ):

                if self.live:

                    if (
                        method
                        == "item/started"
                    ):
                        _CX2_TERMINAL.web_search_started(
                            item
                        )

                    else:
                        _CX2_TERMINAL.web_search_completed(
                            item
                        )

                return

        # ---------------------------------------------
        # Raw response item completion
        #
        # App Server exposes the underlying ResponseItem through
        # rawResponseItem/completed. For final assistant messages
        # this is the closest live representation of the model's
        # completed response item.
        #
        # Normal delta streaming remains preferred. Only populate
        # and render when no agent text has already been received.
        # ---------------------------------------------

        if (
            method
            == "rawResponseItem/completed"
        ):

            raw_final_text = _cx2_extract_raw_response_final_answer(
                params.get("item")
            )

            if raw_final_text is not None:
                self._set_raw_authoritative_final(
                    result,
                    raw_final_text,
                )

            return

        # ---------------------------------------------
        # Agent answer stream
        # ---------------------------------------------

        if (
            method
            == "item/agentMessage/delta"
        ):

            self._handle_agent_delta(
                result,
                params,
            )

            return

        # ---------------------------------------------
        # Command stdout/stderr stream
        # ---------------------------------------------

        if (
            method
            == "item/commandExecution/outputDelta"
        ):

            item_id = params.get(
                "itemId"
            )

            delta = params.get(
                "delta"
            )

            if (
                isinstance(
                    item_id,
                    str,
                )
                and isinstance(
                    delta,
                    str,
                )
            ):

                if item_id not in result.command_accumulators:
                    result.command_accumulators[item_id] = BoundedDiagnosticAccumulator(
                        max_total_bytes=MAX_COMMAND_OUTPUT_BYTES_RETAINED,
                        max_head_bytes=MAX_HEAD_BYTES,
                    )

                result.command_accumulators[item_id].push(delta)
                updated_text = result.command_accumulators[item_id].get_diagnostic_text()
                result.command_output[item_id] = updated_text

                # Resilient late-event audit reconciliation if item/completed was already processed.
                # Strictly fail-closed: late stream deltas update diagnostic/audit text in place,
                # but NEVER reopen a finalized authorization decision or present new bounded-host offers.
                for cmd_exec in result.command_executions:
                    if cmd_exec.get("id") == item_id:
                        cmd_exec["classification_text"] = updated_text
                        if updated_text.strip():
                            cmd_exec["output_snippet"] = updated_text.strip()[:500]

                if self.live:
                    _CX2_TERMINAL.command_output_delta(
                        item_id,
                        delta,
                    )

            return

        # ---------------------------------------------
        # Turn-level latest unified diff
        # ---------------------------------------------

        if (
            method
            == "turn/diff/updated"
        ):

            diff = params.get(
                "diff"
            )

            if isinstance(
                diff,
                str,
            ):
                result.latest_diff = (
                    diff
                )
                result.event_sequence += 1
                diff_files = extract_changed_files_from_diff(diff, repo_root=getattr(self, "cwd", None))
                for df in diff_files:
                    if df.lower() not in [cf.lower() for cf in result.changed_files]:
                        result.changed_files.append(df)
                if diff_files:
                    result.last_mutation_sequence = result.event_sequence

                if self.live:
                    _CX2_TERMINAL.diff_updated(
                        diff
                    )

            return

        # ---------------------------------------------
        # Token telemetry
        #
        # Keep exact generated payload raw.
        # Production telemetry adapter can consume
        # tokenUsage.last/total later.
        # ---------------------------------------------

        if (
            method
            == "thread/tokenUsage/updated"
        ):

            usage = params.get(
                "tokenUsage"
            )

            if isinstance(
                usage,
                dict,
            ):
                result.token_usage = (
                    usage
                )

            return

        # ---------------------------------------------
        # Item lifecycle
        # ---------------------------------------------

        if (
            method
            == "item/started"
        ):

            raw_started_item = params.get(
                "item"
            )

            started_agent_state = None
            is_started_final_candidate = False
            if isinstance(raw_started_item, dict):
                is_started_final_candidate = (
                    raw_started_item.get("type") == "agentMessage"
                    and raw_started_item.get("phase") == "final_answer"
                )
                started_agent_state = self._classify_agent_item(
                    result,
                    raw_started_item,
                    lifecycle="started",
                )
                if is_started_final_candidate and started_agent_state is None:
                    return

            summary = safe_item_summary(
                raw_started_item
            )
            if is_started_final_candidate:
                self._bound_final_candidate_summary(summary)

            result.event_sequence += 1
            if (
                not is_started_final_candidate
                or started_agent_state is None
                or not started_agent_state.started_summary_recorded
            ):
                result.started_items.append(
                    summary
                )
                if started_agent_state is not None:
                    started_agent_state.started_summary_recorded = True

            started_type = summary.get("type")
            if started_type == "fileChange":
                result.last_mutation_sequence = result.event_sequence
                if isinstance(raw_started_item, dict):
                    started_files = extract_changed_files_from_items([raw_started_item], repo_root=getattr(self, "cwd", None))
                    for sf in started_files:
                        if sf.lower() not in [cf.lower() for cf in result.changed_files]:
                            result.changed_files.append(sf)

            if (
                self.live
                and started_type
                == "commandExecution"
            ):

                command = summary.get(
                    "command"
                )

                if command:
                    _CX2_TERMINAL.command_started(
                        str(command)
                    )

            return

        if (
            method
            == "item/completed"
        ):

            completed_item = params.get(
                "item"
            )

            completed_agent_state = None
            is_completed_final_candidate = False
            if isinstance(completed_item, dict):
                is_completed_final_candidate = (
                    completed_item.get("type") == "agentMessage"
                    and completed_item.get("phase") == "final_answer"
                )
                completed_agent_state = self._classify_agent_item(
                    result,
                    completed_item,
                    lifecycle="completed",
                )
                if is_completed_final_candidate and completed_agent_state is None:
                    return

            item_id = (
                str(
                    completed_item.get("id")
                    or ""
                )
                if isinstance(
                    completed_item,
                    dict,
                )
                else ""
            )

            accum = result.command_accumulators.get(item_id)
            if accum is None:
                accum = result.command_output.get(item_id, "")

            completed_summary = (
                safe_item_summary(
                    completed_item,
                    accumulated_stream=accum,
                )
            )
            if is_completed_final_candidate:
                self._bound_final_candidate_summary(completed_summary)

            result.event_sequence += 1
            if (
                not is_completed_final_candidate
                or completed_agent_state is None
                or not completed_agent_state.completed_summary_recorded
            ):
                result.completed_items.append(
                    completed_summary
                )
                if completed_agent_state is not None:
                    completed_agent_state.completed_summary_recorded = True

            completed_type = (
                completed_summary.get(
                    "type"
                )
            )

            if completed_type == "fileChange":
                result.last_mutation_sequence = result.event_sequence
                if isinstance(completed_item, dict):
                    item_files = extract_changed_files_from_items([completed_item], repo_root=getattr(self, "cwd", None))
                    for f in item_files:
                        if f.lower() not in [cf.lower() for cf in result.changed_files]:
                            result.changed_files.append(f)

            elif completed_type == "commandExecution":
                cmd_str = str(completed_summary.get("command") or "")
                exit_code = completed_summary.get("exitCode")
                dur_ms = completed_summary.get("durationMs")
                cats = classify_command(cmd_str)
                masked = is_command_masked(cmd_str)
                disp_cmd = unwrap_display_command(cmd_str)
                raw_out = extract_command_diagnostic_text(
                    completed_item,
                    accumulated_stream=accum,
                )
                cmd_cwd = completed_summary.get("cwd")

                cmd_record = {
                    "id": item_id,
                    "command": cmd_str,
                    "exit_code": exit_code,
                    "duration_ms": dur_ms,
                    "sequence": result.event_sequence,
                    "categories": cats,
                    "is_masked": masked,
                    "display_command": disp_cmd,
                    "output_snippet": completed_summary.get("output_snippet", ""),
                    "classification_text": raw_out,
                    "cwd": cmd_cwd,
                    "item_completed": True,
                    "decision_finalized": True,
                    "bounded_host_execution": False,
                    "bounded_offer_presented": False,
                }
                result.command_executions.append(cmd_record)

                summary_obj = CommandExecutionSummary(
                    command=cmd_str,
                    exit_code=exit_code,
                    duration_ms=dur_ms,
                    sequence=result.event_sequence,
                    categories=cats,
                    is_masked=masked,
                    display_command=disp_cmd,
                    output_snippet=completed_summary.get("output_snippet", ""),
                    classification_text=raw_out,
                    cwd=cmd_cwd,
                    bounded_host_execution=False,
                )

                perms = getattr(self, "current_permissions", ":read-only")
                if is_verification_command_eligible(summary_obj, permissions=perms):
                    cmd_record["bounded_offer_presented"] = True
                    effective_dir = cmd_cwd or str(getattr(self, "current_cwd", None) or Path.cwd())
                    self._handle_bounded_verification_offer(
                        result=result,
                        cmd_str=cmd_str,
                        disp_cmd=disp_cmd,
                        cwd=effective_dir,
                        raw_record=cmd_record,
                    )

            if (
                self.live
                and completed_type
                == "commandExecution"
            ):
                _CX2_TERMINAL.command_completed(
                    completed_summary
                )

            if (
                completed_type
                == "agentMessage"
                and isinstance(completed_item, dict)
                and completed_item.get("phase") == "final_answer"
            ):
                completed_text = (
                    safe_agent_message_text(
                        completed_item
                    )
                )

                if (
                    completed_text is not None
                    and completed_agent_state is not None
                    and item_id
                ):
                    self._set_authoritative_final(
                        result,
                        text=completed_text,
                        source="item/completed",
                        item_id=item_id,
                    )

            return

        # ---------------------------------------------
        # Reasoning
        #
        # Never display or persist raw reasoning text.
        # Only count event volume.
        # ---------------------------------------------

        if method in {
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
        }:

            result.reasoning_event_count += 1

            delta = params.get(
                "delta"
            )

            if isinstance(
                delta,
                str,
            ):
                result.reasoning_delta_chars += (
                    len(
                        delta
                    )
                )

            return

        # ---------------------------------------------
        # Warning
        # ---------------------------------------------

        if method == "warning":

            message = params.get(
                "message"
            )

            if isinstance(
                message,
                str,
            ):
                result.warnings.append(
                    message
                )

                if self.live:
                    _CX2_TERMINAL.warning(
                        message
                    )

            return

        # ---------------------------------------------
        # Turn-scoped error notification
        # ---------------------------------------------

        if method == "error":

            error = params.get(
                "error"
            )

            result.errors.append(
                {
                    "error":
                        error,

                    "willRetry":
                        params.get(
                            "willRetry"
                        ),
                }
            )

            if self.live:
                _CX2_TERMINAL.error(
                    error
                )

            return

        # ---------------------------------------------
        # Turn start
        # ---------------------------------------------

        if (
            method
            == "turn/started"
        ):

            turn = params.get(
                "turn"
            )

            if isinstance(
                turn,
                dict,
            ):

                result.status = str(
                    turn.get(
                        "status",
                        result.status,
                    )
                )

                result.started_at = (
                    turn.get(
                        "startedAt",
                        result.started_at,
                    )
                )

            if self.live:
                _CX2_TERMINAL.turn_started()

            return

        # ---------------------------------------------
        # Turn completion
        # ---------------------------------------------

        if (
            method
            == "turn/completed"
        ):

            turn = params.get(
                "turn"
            )

            if not isinstance(
                turn,
                dict,
            ):
                return

            turn_id = turn.get(
                "id"
            )

            if turn_id != result.turn_id:
                return

            status = str(
                turn.get(
                    "status",
                    "failed",
                )
            )

            result.status = status

            error = turn.get(
                "error"
            )

            result.error = (
                error
                if isinstance(
                    error,
                    dict,
                )
                else None
            )

            result.started_at = (
                turn.get(
                    "startedAt",
                    result.started_at,
                )
            )

            result.completed_at = (
                turn.get(
                    "completedAt"
                )
            )

            duration = turn.get(
                "durationMs"
            )

            result.duration_ms = (
                int(
                    duration
                )
                if isinstance(
                    duration,
                    (int, float),
                )
                else None
            )

            result.final_turn = (
                turn
            )

            if status == "completed":
                for final_item_id, direct_final_text in (
                    _cx2_agent_final_candidates(turn.get("items"))
                ):
                    self._set_authoritative_final(
                        result,
                        text=direct_final_text,
                        source="turn/completed",
                        item_id=final_item_id,
                    )

            # If the Turn payload does not contain a usable final
            # agentMessage, run_turn() still owns the bounded post-wait
            # thread/read fallback installed by 11M-K.
            return


__all__ = [
    "AgentMessageItemState",
    "FINAL_CANDIDATE_EVIDENCE_MAX_BYTES",
    "MAX_FINAL_ANSWER_CANDIDATES",
    "MAX_FINAL_CANDIDATE_ITEM_ID_BYTES",
    "MAX_INTERACTIVE_APPROVAL_PROMPTS_PER_TURN",
    "MAX_UNRESOLVED_PRESTART_ITEMS",
    "StreamingTurnRunner",
    "TurnApprovalState",
    "TurnRunResult",
    "UNRESOLVED_ITEM_MAX_BYTES",
    "UNRESOLVED_TURN_MAX_BYTES",
]
