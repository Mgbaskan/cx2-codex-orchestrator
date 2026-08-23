from __future__ import annotations

from dataclasses import (
    asdict,
    dataclass,
    field,
)

from pathlib import Path

import sys
import time

from typing import (
    Any,
    Protocol,
)


CX_HOME = Path.home() / ".cx"
STAGE = CX_HOME / "runtime" / "cx2"

if str(STAGE) not in sys.path:
    sys.path.insert(
        0,
        str(STAGE),
    )

from verification_gate import (
    classify_command,
    extract_changed_files_from_diff,
    extract_changed_files_from_items,
    is_command_masked,
    unwrap_display_command,
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

    latest_diff: str = ""

    token_usage: dict[str, Any] | None = None

    command_output: dict[
        str,
        str,
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
        ) and value:
            return value

    # Be tolerant of structured message content.
    content = item.get(
        "content"
    )

    if isinstance(
        content,
        str,
    ) and content:
        return content

    if not isinstance(
        content,
        list,
    ):
        return None

    parts: list[str] = []

    for entry in content:

        if isinstance(
            entry,
            str,
        ):
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
        ) and value:
            parts.append(
                value
            )

    if not parts:
        return None

    return "".join(
        parts
    )


def safe_item_summary(
    item: Any,
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
        raw_output = item.get("output") or item.get("error") or item.get("stderr") or ""
        if isinstance(raw_output, str) and raw_output.strip():
            result["output_snippet"] = raw_output.strip()[:500]

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
def _cx2_extract_thread_final_answer(
    payload: Any,
    *,
    expected_turn_id: str,
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

        items = turn.get(
            "items"
        )

        if not isinstance(
            items,
            list,
        ):
            return None

        final_text = None

        for item in items:

            if not isinstance(
                item,
                dict,
            ):
                continue

            if item.get(
                "type"
            ) != "agentMessage":
                continue

            if item.get(
                "phase"
            ) != "final_answer":
                continue

            value = item.get(
                "text"
            )

            if isinstance(
                value,
                str,
            ) and value:
                final_text = value

        return final_text

    return None



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

        value = entry.get(
            "text"
        )

        if isinstance(
            value,
            str,
        ) and value:
            parts.append(
                value
            )

    if not parts:
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
      item.text is non-empty str

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

    final_text: str | None = None

    for item in items:

        if not isinstance(
            item,
            dict,
        ):
            continue

        if item.get(
            "type"
        ) != "agentMessage":
            continue

        if item.get(
            "phase"
        ) != "final_answer":
            continue

        value = item.get(
            "text"
        )

        if isinstance(
            value,
            str,
        ) and value:
            final_text = value

    return final_text


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
    ) -> None:

        self.client = client
        self.live = live

        if self.live:
            _configure_live_stdio_safety()

        self.poll_interval = max(
            0.005,
            float(
                poll_interval
            ),
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
        timeout: float = 300.0,
    ) -> TurnRunResult:

        # CX2_TURN_APPROVAL_POLICY_ARG_V1
        if approval_policy not in {
            "never",
            "on-request",
        }:
            raise ValueError(
                "Unsupported CX2 approval policy: "
                + repr(approval_policy)
            )

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

            if (
                status
                == "completed"
                and not result.agent_text
            ):
                self._recover_final_answer_from_thread(
                    result
                )

            return result

        try:
            completed_result = self.wait_for_turn(
                result,
                timeout=timeout,
            )
        except KeyboardInterrupt:
            if result.status not in FINAL_STATUSES:
                result.status = "interrupted"
            raise
        except Exception:
            if result.status not in FINAL_STATUSES:
                result.status = "failed"
            raise

        if (
            completed_result.status
            == "completed"
            and not completed_result.agent_text
        ):
            self._recover_final_answer_from_thread(
                completed_result
            )

        return completed_result

    # ---------------------------------------------------------
    # Event loop
    # ---------------------------------------------------------

    def wait_for_turn(
        self,
        result: TurnRunResult,
        *,
        timeout: float,
    ) -> TurnRunResult:

        deadline = (
            time.monotonic()
            + timeout
        )

        interrupted_once = False

        while True:

            try:
                # Server requests FIRST.
                #
                # A server-side approval may pause the turn
                # until we respond.
                for request in (
                    self.client
                    .drain_server_requests()
                ):
                    self._handle_server_request(
                        result,
                        request,
                    )

                for notification in (
                    self.client
                    .drain_notifications()
                ):
                    self._handle_notification(
                        result,
                        notification,
                    )

                unknown = (
                    self.client
                    .drain_unknown()
                )

                if unknown:
                    result.unknown_messages.extend(
                        unknown
                    )

                if (
                    result.status
                    in FINAL_STATUSES
                ):
                    return result

                if (
                    time.monotonic()
                    >= deadline
                ):
                    try:
                        self.interrupt(
                            result.thread_id,
                            result.turn_id,
                        )
                    except Exception:
                        pass
                    if result.status not in FINAL_STATUSES:
                        result.status = "failed"
                    raise TimeoutError(
                        "turn/completed timeout: "
                        f"{result.turn_id}"
                    )

                time.sleep(
                    self.poll_interval
                )

            except KeyboardInterrupt:

                if interrupted_once:
                    if result.status not in FINAL_STATUSES:
                        result.status = "interrupted"
                    raise

                interrupted_once = True
                result.interrupt_requested = True

                if self.live:
                    _CX2_TERMINAL.interrupting()

                try:
                    self.interrupt(
                        result.thread_id,
                        result.turn_id,
                    )
                except Exception:
                    pass

                # Give App Server time to emit
                # turn/completed status=interrupted.
                deadline = max(
                    deadline,
                    time.monotonic()
                    + 15.0,
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

        def safe_prompt(
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

            if not self.live:
                return safe_default

            decision = _CX2_TERMINAL.approval_prompt(
                title=title,
                details=details,
                decisions=decisions,
                default_decision=safe_default,
            )

            if decision not in decisions:
                return safe_default

            return decision

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

            self.client.respond(
                request_id,
                {
                    "decision":
                        decision,
                },
            )

            record(
                decision
            )

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

            decision = safe_prompt(
                title="File changes",
                details=details,
                decisions=decisions,
                default="decline",
            )

            self.client.respond(
                request_id,
                {
                    "decision":
                        decision,
                },
            )

            record(
                decision
            )

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

            if isinstance(
                command,
                list,
            ):
                command_text = " ".join(
                    str(part)
                    for part in command
                )

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

            decision = safe_prompt(
                title="Command execution",
                details=details,
                decisions=decisions,
                default="denied",
            )

            self.client.respond(
                request_id,
                {
                    "decision":
                        decision,
                },
            )

            record(
                decision
            )

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

            decision = safe_prompt(
                title="File changes",
                details=details,
                decisions=decisions,
                default="denied",
            )

            self.client.respond(
                request_id,
                {
                    "decision":
                        decision,
                },
            )

            record(
                decision
            )

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

        Normal streaming bypasses this method entirely when agent_text
        is already populated.
        """

        if result.agent_text:
            return result.agent_text

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

            if result.agent_text:
                return result.agent_text

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

            value = _cx2_extract_thread_final_answer(
                response,
                expected_turn_id=result.turn_id,
            )

            if value:
                result.agent_text = value

                if self.live:
                    _CX2_TERMINAL.agent_delta(
                        value
                    )

                return value

            if (
                attempt_index
                + 1
                < attempt_count
                and delay > 0.0
            ):
                time.sleep(
                    delay
                )

        if last_error is not None:
            result.warnings.append(
                "Final answer thread/read fallback failed after "
                + str(attempt_count)
                + " attempts: "
                + repr(last_error)
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

            if not result.agent_text:

                raw_final_text = (
                    _cx2_extract_raw_response_final_answer(
                        params.get(
                            "item"
                        )
                    )
                )

                if raw_final_text:
                    result.agent_text = (
                        raw_final_text
                    )

                    if self.live:
                        _CX2_TERMINAL.agent_delta(
                            raw_final_text
                        )

            return

        # ---------------------------------------------
        # Agent answer stream
        # ---------------------------------------------

        if (
            method
            == "item/agentMessage/delta"
        ):

            delta = params.get(
                "delta"
            )

            if isinstance(
                delta,
                str,
            ):

                result.agent_text += (
                    delta
                )

                if self.live:
                    _CX2_TERMINAL.agent_delta(
                        delta
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

                result.command_output[
                    item_id
                ] = (
                    result.command_output.get(
                        item_id,
                        "",
                    )
                    + delta
                )

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

            summary = safe_item_summary(
                raw_started_item
            )

            result.event_sequence += 1
            result.started_items.append(
                summary
            )

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

            completed_summary = (
                safe_item_summary(
                    completed_item
                )
            )

            result.event_sequence += 1
            result.completed_items.append(
                completed_summary
            )

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
                raw_out = ""
                if isinstance(completed_item, dict):
                    raw_out = str(completed_item.get("output") or completed_item.get("error") or completed_item.get("stderr") or "")
                result.command_executions.append({
                    "command": cmd_str,
                    "exit_code": exit_code,
                    "duration_ms": dur_ms,
                    "sequence": result.event_sequence,
                    "categories": cats,
                    "is_masked": masked,
                    "display_command": disp_cmd,
                    "output_snippet": completed_summary.get("output_snippet", ""),
                    "classification_text": raw_out,
                    "cwd": completed_summary.get("cwd"),
                })

            if (
                self.live
                and completed_type
                == "commandExecution"
            ):
                _CX2_TERMINAL.command_completed(
                    completed_summary
                )

            # Completion fallback:
            #
            # Some App Server turns expose the final agent message
            # through item/completed without emitting an
            # item/agentMessage/delta notification.
            #
            # Only use the fallback when result.agent_text is empty.
            # This guarantees normal streamed answers are not printed
            # twice.
            if (
                completed_type
                == "agentMessage"
                and not result.agent_text
            ):
                completed_text = (
                    safe_agent_message_text(
                        completed_item
                    )
                )

                if completed_text:
                    result.agent_text = (
                        completed_text
                    )

                    if self.live:
                        _CX2_TERMINAL.agent_delta(
                            completed_text
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

            if (
                isinstance(
                    turn_id,
                    str,
                )
                and turn_id
                != result.turn_id
            ):
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

            # Prefer the final agentMessage carried directly by the
            # generated turn/completed Turn payload. This avoids the
            # thread persistence race entirely.
            if (
                status
                == "completed"
                and not result.agent_text
            ):
                direct_final_text = (
                    _cx2_extract_turn_final_answer(
                        turn
                    )
                )

                if direct_final_text:
                    result.agent_text = (
                        direct_final_text
                    )

                    if self.live:
                        _CX2_TERMINAL.agent_delta(
                            direct_final_text
                        )

            # If the Turn payload does not contain a usable final
            # agentMessage, run_turn() still owns the bounded post-wait
            # thread/read fallback installed by 11M-K.
            if self.live:
                _CX2_TERMINAL.turn_completed(
                    status
                )

            return


__all__ = [
    "StreamingTurnRunner",
    "TurnRunResult",
]
