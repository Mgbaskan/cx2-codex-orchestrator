from __future__ import annotations

import io
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _bootstrap  # noqa: E402
sys.path.insert(0, str(_bootstrap.RUNTIME_DIR))

from client import AppServerClient  # noqa: E402
from cx2_runtime import CX2Runtime  # noqa: E402
from file_write_grants import (  # noqa: E402
    FileWriteGrantRegistry,
    ordinary_workspace_file_mutation,
)
from prompt_transport import MAX_PROMPT_BYTES, capture_multiline_paste  # noqa: E402
from release_version import CX2_VERSION  # noqa: E402
from terminal_markdown import TerminalMarkdownStream  # noqa: E402
from terminal_pager import TerminalPager, pager_capable, wrap_display  # noqa: E402
from terminal_ui import TerminalRenderer  # noqa: E402
from transcript_store import MAX_RESPONSE_BYTES, TranscriptStore  # noqa: E402
from turn_runner import StreamingTurnRunner, TurnRunResult  # noqa: E402


class TTY(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def semantic_terminal_text(value: str) -> str:
    return _ANSI_SGR_RE.sub("", value)


class ApprovalClient:
    def __init__(self) -> None:
        self.responses: list[tuple[object, object]] = []

    def respond(self, request_id, result) -> None:
        self.responses.append((request_id, result))

    def respond_error(self, request_id, code, message) -> None:
        self.responses.append((request_id, {"error": {"code": code, "message": message}}))


class CountingTerminal:
    def __init__(self) -> None:
        self.visible_bytes = 0
        self.visible_digest = hashlib.sha256()
        self.reconciled: list[str] = []
        self.warnings: list[str] = []

    def agent_delta(self, value: str) -> None:
        encoded = value.encode("utf-8")
        self.visible_bytes += len(encoded)
        self.visible_digest.update(encoded)

    def response_reconciled(self, value: str) -> None:
        self.reconciled.append(value)

    def confirm_empty_response(self) -> None:
        return None

    def warning(self, value: str) -> None:
        self.warnings.append(value)


class TestTerminalSecurityAndLifecycle2014(unittest.TestCase):
    def test_untrusted_terminal_controls_are_visible_but_inert(self) -> None:
        from terminal_safety import sanitize_untrusted_text

        raw = "ok\x1b[2J\x1b]8;;https://example.test\x07link\x1b]8;;\x07\x1b]52;c;QQ==\x07\rX\x00\x03"
        presented = sanitize_untrusted_text(raw)
        self.assertNotIn("\x1b", presented)
        self.assertNotIn("\x07", presented)
        self.assertNotIn("\r", presented)
        self.assertNotIn("\x00", presented)
        self.assertIn(r"\x1b", presented)
        self.assertIn(r"\x07", presented)
        self.assertIn(r"\r", presented)
        self.assertIn(r"\x00", presented)

    def test_antigravity_sticky_survives_command_boundaries_with_or_without_color(self) -> None:
        for no_color in (False, True):
            with self.subTest(no_color=no_color):
                stream = TTY()
                env = {"TERM": "", "TERM_PROGRAM": "vscode", "WT_SESSION": ""}
                if no_color:
                    env["NO_COLOR"] = ""
                with patch.object(sys, "stdin", TTY()), patch.dict(os.environ, env, clear=True):
                    renderer = TerminalRenderer(stream=stream)
                    renderer.render_turn_header(
                        session_mode="resume", model="model", effort="high",
                        sandbox="workspace-write",
                        quota={"available": True, "remainingPercent": 4, "state": "EMERGENCY"},
                    )
                    self.assertTrue(renderer.capabilities.cursor)
                    self.assertTrue(renderer.capabilities.sticky_status)
                    self.assertEqual(renderer.capabilities.color, not no_color)
                    renderer.turn_started()
                    renderer.command_started("Get-Content x")
                    self.assertTrue(renderer._status_visible)
                    renderer.command_completed({
                        "command": "Get-Content x", "status": "completed",
                        "exitCode": 0, "durationMs": 10,
                    })
                    self.assertTrue(renderer._status_visible)
                    renderer.interrupted()
                    self.assertTrue(renderer._status_visible)
                    renderer.close()

    def test_static_ui_disables_pager_cursor_bytes(self) -> None:
        stream = TTY()
        with patch.dict(os.environ, {"TERM": "xterm", "CX2_STATIC_UI": "1"}, clear=True):
            self.assertFalse(pager_capable(stream, TTY()))
            TerminalPager(stream=stream, input_stream=TTY()).show("a\nb\nc")
        self.assertNotIn("\x1b", stream.getvalue())

    def test_pager_restores_sticky_owner(self) -> None:
        stream = TTY()
        renderer = TerminalRenderer(stream=stream)
        renderer.set_status_snapshot(model="m", effort="low", sandbox="read-only")
        with patch.object(sys, "stdin", TTY()), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            renderer.render_status_line()
            previous = renderer.suspend_presentation("pager")
            renderer.restore_presentation(previous)
        self.assertTrue(renderer._status_visible)

    def test_control_safety_is_applied_to_tty_non_tty_and_pager(self) -> None:
        dangerous = "A\x1b[2J\x1b]52;c;QQ==\x07\rB"
        for stream in (TTY(), io.StringIO()):
            with self.subTest(tty=stream.isatty()), patch.dict(
                os.environ, {"NO_COLOR": "1", "TERM": "dumb"}, clear=True
            ):
                renderer = TerminalRenderer(stream=stream)
                renderer.agent_delta(dangerous)
                renderer.turn_completed("completed")
                emitted = stream.getvalue()
                self.assertNotIn("\x1b", emitted)
                self.assertNotIn("\x07", emitted)
                self.assertNotIn("\r", emitted)
                self.assertIn(r"\x1b", emitted)
        paged = io.StringIO()
        TerminalPager(stream=paged, input_stream=io.StringIO()).show(dangerous)
        self.assertNotIn("\x1b", paged.getvalue())
        self.assertIn(r"\x1b", paged.getvalue())

    def test_response_and_approval_restore_deliberate_status_state(self) -> None:
        stream = TTY()
        with patch.object(sys, "stdin", TTY()), patch.dict(
            os.environ, {"TERM_PROGRAM": "vscode", "NO_COLOR": ""}, clear=True
        ):
            renderer = TerminalRenderer(stream=stream)
            renderer.set_status_snapshot(
                quota={"available": True, "remainingPercent": 4, "state": "EMERGENCY"},
                model="m", effort="high", sandbox="read-only",
            )
            renderer.render_status_line()
            renderer.agent_delta("answer")
            self.assertFalse(renderer._status_visible)
            renderer.turn_completed("completed")
            self.assertTrue(renderer._status_visible)
            with patch("builtins.input", return_value="3"):
                decision = renderer.approval_prompt(
                    title="File", details=["safe"],
                    decisions=["accept", "decline"], default_decision="decline",
                )
            self.assertEqual(decision, "decline")
            self.assertTrue(renderer._status_visible)

    def test_large_aggregate_diff_uses_full_identity_not_retained_tail(self) -> None:
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        first = "FIRST-ONLY\n" + ("x" * (300 * 1024))
        renderer.diff_updated(first)
        renderer.diff_updated(first + "\nSECOND-ONLY\n")
        emitted = stream.getvalue()
        self.assertEqual(emitted.count("FIRST-ONLY"), 1)
        self.assertEqual(emitted.count("SECOND-ONLY"), 1)


class TestCommandIdempotency2014(unittest.TestCase):
    def setUp(self) -> None:
        self.out = TTY()
        self.runner = StreamingTurnRunner(ApprovalClient(), live=True)
        self.result = TurnRunResult(thread_id="thread", turn_id="turn")

    def event(
        self,
        phase: str,
        *,
        item_id: str = "item-1",
        command: str = "Get-Content x",
        duration: int = 10,
    ) -> dict:
        return {
            "method": f"item/{phase}",
            "params": {
                "threadId": "thread", "turnId": "turn",
                "item": {
                    "id": item_id, "type": "commandExecution", "command": command,
                    "cwd": str(ROOT), "status": "completed", "exitCode": 0,
                    "durationMs": duration,
                },
            },
        }

    def test_identical_started_and_completed_events_are_idempotent(self) -> None:
        cases = (
            ("tty-color", TTY, {"TERM": "xterm"}),
            ("tty-no-color", TTY, {"TERM": "xterm", "NO_COLOR": "1"}),
            ("tty-static", TTY, {"TERM": "xterm", "CX2_STATIC_UI": "1"}),
            ("non-tty", io.StringIO, {"TERM": "dumb"}),
        )
        for name, stream_factory, environment in cases:
            with self.subTest(name=name):
                stream = stream_factory()
                runner = StreamingTurnRunner(ApprovalClient(), live=True)
                result = TurnRunResult(thread_id="thread", turn_id="turn")
                started = self.event("started")
                completed = self.event("completed")
                with patch.dict(os.environ, environment, clear=True), patch(
                    "turn_runner._CX2_TERMINAL", TerminalRenderer(stream=stream)
                ):
                    for event in (started, started, completed, completed):
                        runner._handle_notification(result, event)

                rendered = semantic_terminal_text(stream.getvalue())
                self.assertEqual(len(result.command_executions), 1)
                self.assertEqual(list(result.command_execution_index), ["item-1"])
                self.assertIs(
                    result.command_execution_index["item-1"],
                    result.command_executions[0],
                )
                self.assertEqual(result.command_event_duplicate_count, 2)
                self.assertEqual(len(result.command_event_fingerprints), 2)
                self.assertEqual(rendered.count("> Get-Content x"), 1)
                self.assertEqual(rendered.count("[ok]"), 1)
                if name == "non-tty":
                    self.assertNotIn("\x1b", stream.getvalue())

                runtime = CX2Runtime(live=False)
                runtime._capture_trace(result)
                self.assertEqual(len(runtime.last_trace), 1)
                self.assertEqual(runtime.last_trace[0]["command"], "Get-Content x")

    def test_conflicting_same_identity_fails_diagnostically(self) -> None:
        with patch(
            "turn_runner._CX2_TERMINAL", TerminalRenderer(stream=io.StringIO())
        ):
            self.runner._handle_notification(self.result, self.event("completed", duration=10))
            self.runner._handle_notification(self.result, self.event("completed", duration=11))
        self.assertEqual(self.result.status, "failed")
        self.assertEqual(self.result.protocol_failure_reason, "COMMAND_EVENT_IDENTITY_CONFLICT")
        self.assertEqual(len(self.result.command_executions), 1)

    def test_different_command_identities_remain_distinct(self) -> None:
        stream = TTY()
        with patch.dict(os.environ, {"TERM": "xterm"}, clear=True), patch(
            "turn_runner._CX2_TERMINAL", TerminalRenderer(stream=stream)
        ):
            for item_id, command in (
                ("item-1", "Get-Content x"),
                ("item-2", "Get-Content y"),
            ):
                self.runner._handle_notification(
                    self.result,
                    self.event("started", item_id=item_id, command=command),
                )
                self.runner._handle_notification(
                    self.result,
                    self.event("completed", item_id=item_id, command=command),
                )

        rendered = semantic_terminal_text(stream.getvalue())
        self.assertEqual(len(self.result.command_executions), 2)
        self.assertEqual(set(self.result.command_execution_index), {"item-1", "item-2"})
        self.assertEqual(rendered.count("> Get-Content x"), 1)
        self.assertEqual(rendered.count("> Get-Content y"), 1)
        self.assertEqual(rendered.count("[ok]"), 2)


class TestWindowsGrantHardening2014(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT

    def eligible(self, path: str, action: str = "edit") -> bool:
        return ordinary_workspace_file_mutation(
            {"fileChanges": [{"path": path, "action": action}]},
            workspace_root=self.root,
        )

    def test_reserved_devices_namespaces_ads_and_ambiguous_paths_rejected(self) -> None:
        bad = [
            "CON", "con.txt", "PRN", "AUX.log", "NUL", "CLOCK$",
            *[f"COM{i}.txt" for i in range(1, 10)],
            *[f"lpt{i}" for i in range(1, 10)],
            "file.txt:stream", r"\\?\C:\temp\x", r"\\.\NUL",
            r"C:relative.txt", "trailing. ", "trailing.", "../escape.txt",
        ]
        for value in bad:
            with self.subTest(value=value):
                self.assertFalse(self.eligible(value))

    def test_structured_operation_not_human_text_controls_destructiveness(self) -> None:
        self.assertTrue(self.eligible("delete-notes.txt"))
        self.assertTrue(
            ordinary_workspace_file_mutation(
                {"reason": "document git clean safely", "fileChanges": [{"path": "notes.txt", "action": "edit", "content": "git reset --hard is dangerous"}]},
                workspace_root=self.root,
            )
        )
        self.assertFalse(self.eligible("notes.txt", action="delete"))

    def test_ineligible_request_cannot_send_accept_for_session(self) -> None:
        client = ApprovalClient()
        runner = StreamingTurnRunner(client, live=True)
        runner.current_cwd = self.root
        runner._active_thread_id = "thread"
        result = TurnRunResult(thread_id="thread", turn_id="turn")
        request = {
            "id": "request", "method": "item/fileChange/requestApproval",
            "params": {
                "fileChanges": [{"path": "CON", "action": "edit"}],
                "availableDecisions": ["accept", "acceptForSession", "decline"],
            },
        }
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="acceptForSession"), patch.object(
            TerminalRenderer, "can_prompt", new=property(lambda self: True)
        ):
            runner._handle_server_request(result, request)
        self.assertNotEqual(client.responses[-1][1], {"decision": "acceptForSession"})

    def test_mixed_safe_and_unsafe_targets_fail_closed(self) -> None:
        self.assertFalse(
            ordinary_workspace_file_mutation(
                {"fileChanges": [
                    {"path": "safe.txt", "action": "edit"},
                    {"path": "NUL.txt", "action": "edit"},
                ]},
                workspace_root=self.root,
            )
        )


class TestLegacyApprovalSecurity2014(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ApprovalClient()
        self.registry = FileWriteGrantRegistry("legacy-security")
        self.runner = StreamingTurnRunner(
            self.client,
            live=True,
            file_write_grants=self.registry,
        )
        self.runner.current_cwd = ROOT
        self.runner._active_thread_id = "thread"
        self.result = TurnRunResult(thread_id="thread", turn_id="turn")

    @staticmethod
    def _legacy_request(
        file_changes: object,
        *,
        available: list[str] | None = None,
        request_id: str = "legacy",
        **extra: object,
    ) -> dict:
        params: dict[str, object] = {"fileChanges": file_changes, **extra}
        if available is not None:
            params["availableDecisions"] = available
        return {"id": request_id, "method": "applyPatchApproval", "params": params}

    def _handle(self, request: dict, returned: str):
        with patch(
            "turn_runner._CX2_TERMINAL.approval_prompt",
            return_value=returned,
        ) as prompt, patch.object(
            TerminalRenderer,
            "can_prompt",
            new=property(lambda self: True),
        ):
            self.runner._handle_server_request(self.result, request)
        return prompt

    def test_safe_legacy_session_is_presented_sent_and_recorded(self) -> None:
        request = self._legacy_request(
            {"safe.txt": {"action": "edit"}},
            available=["approved", "approved_for_session", "denied"],
        )
        prompt = self._handle(request, "approved_for_session")
        self.assertIn("approved_for_session", prompt.call_args.kwargs["decisions"])
        self.assertEqual(
            self.client.responses[-1],
            ("legacy", {"decision": "approved_for_session"}),
        )
        self.assertTrue(
            self.registry.has(thread_id="thread", workspace_root=ROOT)
        )

    def test_legacy_never_invents_server_session_decision(self) -> None:
        request = self._legacy_request(
            {"safe.txt": {"action": "edit"}},
            available=["approved", "denied"],
        )
        prompt = self._handle(request, "approved")
        self.assertNotIn("approved_for_session", prompt.call_args.kwargs["decisions"])
        self.assertEqual(self.client.responses[-1], ("legacy", {"decision": "approved"}))
        self.assertFalse(self.registry.has(thread_id="thread", workspace_root=ROOT))

    def test_legacy_ineligible_matrix_never_presents_or_sends_session(self) -> None:
        outside = str(ROOT.parent / "outside.txt")
        cases = {
            "CON": {"CON": {"action": "edit"}},
            "PRN": {"PRN.txt": {"action": "edit"}},
            "AUX": {"AUX": {"action": "edit"}},
            "NUL": {"NUL.log": {"action": "edit"}},
            "COM1": {"COM1": {"action": "edit"}},
            "LPT1": {"LPT1.txt": {"action": "edit"}},
            "ADS": {"ok.txt:stream": {"action": "edit"}},
            "namespace-question": {r"\\?\C:\temp\x": {"action": "edit"}},
            "namespace-device": {r"\\.\NUL": {"action": "edit"}},
            "trailing-dot": {"bad.": {"action": "edit"}},
            "trailing-space": {"bad ": {"action": "edit"}},
            "drive-relative": {"C:relative.txt": {"action": "edit"}},
            "traversal": {r"..\outside.txt": {"action": "edit"}},
            "outside": {outside: {"action": "edit"}},
            "unc-outside": {r"\\server\share\outside.txt": {"action": "edit"}},
            "mixed": {
                "safe.txt": {"action": "edit"},
                "CON": {"action": "edit"},
            },
            "unknown": {"action": "edit"},
            "delete": {"safe.txt": {"action": "delete"}},
            "rename": {"safe.txt": {"action": "rename"}},
        }
        for index, (name, changes) in enumerate(cases.items()):
            with self.subTest(name=name):
                self.result = TurnRunResult(
                    thread_id="thread",
                    turn_id=f"turn-{index}",
                )
                request = self._legacy_request(
                    changes,
                    available=["approved", "approved_for_session", "denied", "abort"],
                    request_id=f"legacy-{index}",
                )
                prompt = self._handle(request, "approved_for_session")
                self.assertNotIn(
                    "approved_for_session",
                    prompt.call_args.kwargs["decisions"],
                )
                self.assertIn(
                    self.client.responses[-1][1].get("decision"),
                    {"denied", "abort"},
                )
                self.assertFalse(
                    self.registry.has(thread_id="thread", workspace_root=ROOT)
                )

    def test_existing_grant_cannot_authorize_later_ineligible_legacy_request(self) -> None:
        self.registry.grant(thread_id="thread", workspace_root=ROOT)
        request = self._legacy_request(
            {"CON": {"action": "edit"}},
            available=["approved", "approved_for_session", "denied"],
        )
        prompt = self._handle(request, "approved_for_session")
        self.assertEqual(prompt.call_count, 1)
        self.assertNotIn("approved_for_session", prompt.call_args.kwargs["decisions"])
        self.assertEqual(self.client.responses[-1], ("legacy", {"decision": "denied"}))

    def test_stale_session_choice_and_recording_failure_fail_closed(self) -> None:
        unsafe = self._legacy_request(
            {"ok.txt:stream": {"action": "edit"}},
            available=["approved_for_session", "denied"],
            request_id="unsafe",
        )
        self._handle(unsafe, "approved_for_session")
        self.assertEqual(self.client.responses[-1], ("unsafe", {"decision": "denied"}))

        safe = self._legacy_request(
            {"safe.txt": {"action": "edit"}},
            available=["approved_for_session", "denied"],
            request_id="record-failure",
        )
        with patch.object(self.registry, "grant", side_effect=OSError("blocked")):
            self._handle(safe, "approved_for_session")
        self.assertIn("error", self.client.responses[-1][1])
        self.assertFalse(self.registry.has(thread_id="thread", workspace_root=ROOT))

    def test_modern_and_legacy_session_policy_are_equivalent(self) -> None:
        cases = [
            ("safe", [{"path": "nested/safe.txt", "action": "edit"}], {"nested/safe.txt": {"action": "edit"}}, True),
            ("unsafe", [{"path": "NUL", "action": "edit"}], {"NUL": {"action": "edit"}}, False),
        ]
        for index, (name, modern_changes, legacy_changes, expected) in enumerate(cases):
            with self.subTest(name=name):
                modern = {
                    "id": f"modern-{index}",
                    "method": "item/fileChange/requestApproval",
                    "params": {
                        "fileChanges": modern_changes,
                        "availableDecisions": ["accept", "acceptForSession", "decline"],
                    },
                }
                modern_prompt = self._handle(modern, "decline")
                legacy = self._legacy_request(
                    legacy_changes,
                    available=["approved", "approved_for_session", "denied"],
                    request_id=f"legacy-parity-{index}",
                )
                legacy_prompt = self._handle(legacy, "denied")
                self.assertEqual(
                    "acceptForSession" in modern_prompt.call_args.kwargs["decisions"],
                    expected,
                )
                self.assertEqual(
                    "approved_for_session" in legacy_prompt.call_args.kwargs["decisions"],
                    expected,
                )

    def test_thread_identity_mismatch_removes_legacy_session_scope(self) -> None:
        self.runner._active_thread_id = "other-thread"
        request = self._legacy_request(
            {"safe.txt": {"action": "edit"}},
            available=["approved", "approved_for_session", "denied"],
        )
        prompt = self._handle(request, "approved_for_session")
        self.assertNotIn("approved_for_session", prompt.call_args.kwargs["decisions"])
        self.assertEqual(self.client.responses[-1], ("legacy", {"decision": "denied"}))


class TestBoundsAndPerformance2014(unittest.TestCase):
    @staticmethod
    def _stream_final(
        deltas: list[str],
        authoritative: str,
        *,
        transcript_store: TranscriptStore | None = None,
    ) -> tuple[StreamingTurnRunner, TurnRunResult, CountingTerminal]:
        runner = StreamingTurnRunner(ApprovalClient(), live=True)
        result = TurnRunResult(thread_id="thread", turn_id="turn")
        terminal = CountingTerminal()
        if transcript_store is not None:
            runner._transcript_sink = transcript_store.start_response(
                thread_id="thread",
                turn_id="turn",
                workspace_key="workspace",
                display_workspace="workspace",
            )
        runner._classify_agent_item(
            result,
            {"id": "answer", "type": "agentMessage", "phase": "final_answer"},
            lifecycle="started",
        )
        with patch("turn_runner._CX2_TERMINAL", terminal):
            for delta in deltas:
                runner._handle_agent_delta(
                    result,
                    {
                        "threadId": "thread",
                        "turnId": "turn",
                        "itemId": "answer",
                        "delta": delta,
                    },
                )
            runner._set_authoritative_final(
                result,
                text=authoritative,
                source="item/completed",
                item_id="answer",
            )
            result.status = "completed"
            runner._finalize_terminal_result(result, allow_recovery=False)
            runner._finalize_transcript(result)
        return runner, result, terminal

    def test_transport_collections_are_bounded(self) -> None:
        client = AppServerClient(Path("missing"))
        self.assertGreater(client.notifications.maxsize, 0)
        self.assertGreater(client.server_requests.maxsize, 0)
        self.assertGreater(client.unknown_messages.maxsize, 0)
        self.assertIsNotNone(getattr(client.stderr_lines, "maxlen", None))

    def test_paste_stops_acquiring_at_incremental_utf8_limit(self) -> None:
        calls = 0

        def source(_prompt: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return "🙂" * ((MAX_PROMPT_BYTES // 4) + 1)
            raise AssertionError("paste acquisition continued after overflow")

        output: list[str] = []
        self.assertIsNone(capture_multiline_paste(input_func=source, print_func=output.append))
        self.assertEqual(calls, 1)
        self.assertIn("sınırı", output[-1])

    def test_markdown_unfinished_line_is_bounded_and_complete(self) -> None:
        stream = TerminalMarkdownStream()
        payload = "Türkçe🙂" * 160000
        rendered = stream.feed(payload)
        rendered += stream.finish()
        self.assertEqual(rendered, payload)
        self.assertLessEqual(stream.buffered_bytes, stream.max_buffer_bytes)

    def test_large_line_wrapping_is_near_linear(self) -> None:
        start = time.perf_counter()
        lines = wrap_display("x" * (1024 * 1024), 100)
        duration = time.perf_counter() - start
        self.assertEqual("".join(lines), "x" * (1024 * 1024))
        self.assertLess(duration, 1.5)

    def test_transport_overflow_is_explicit(self) -> None:
        client = AppServerClient(Path("missing"))
        message = {"method": "event", "params": {}}
        for _ in range(client.notifications.maxsize):
            self.assertTrue(client._put_transport_event(client.notifications, message, "test"))
        self.assertFalse(client._put_transport_event(client.notifications, message, "test"))
        self.assertIsNotNone(client.transport_failure)

    def test_turn_item_capacity_fails_instead_of_silent_loss(self) -> None:
        runner = StreamingTurnRunner(ApprovalClient(), live=False)
        result = TurnRunResult(thread_id="thread", turn_id="turn")
        for index in range(4097):
            runner._handle_notification(result, {
                "method": "item/started",
                "params": {"threadId": "thread", "turnId": "turn", "item": {
                    "id": f"item-{index}", "type": "mcpToolCall", "status": "inProgress",
                }},
            })
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.protocol_failure_reason, "STARTED_ITEM_LIMIT")
        self.assertEqual(len(result.started_items), 4096)

    def test_exactly_16_mib_is_visible_successful_and_durable(self) -> None:
        text = "x" * MAX_RESPONSE_BYTES
        with tempfile.TemporaryDirectory(dir=_bootstrap.TEST_TEMP_ROOT) as temp_dir:
            store = TranscriptStore(Path(temp_dir) / "transcript.sqlite3")
            _runner, result, terminal = self._stream_final(
                [text[index : index + 1024 * 1024] for index in range(0, len(text), 1024 * 1024)],
                text,
                transcript_store=store,
            )
            row = store.get_last(workspace_key="workspace", thread_id="thread")
            self.assertEqual(result.outcome, "COMPLETED")
            self.assertEqual(terminal.visible_bytes, MAX_RESPONSE_BYTES)
            self.assertEqual(row.retained_bytes, MAX_RESPONSE_BYTES)
            self.assertFalse(row.truncated)
            store.close()

    def test_16_mib_plus_later_deltas_continue_and_transcript_truncates(self) -> None:
        deltas = ["x" * (1024 * 1024) for _ in range(16)] + ["y", "later"]
        text = "".join(deltas)
        with tempfile.TemporaryDirectory(dir=_bootstrap.TEST_TEMP_ROOT) as temp_dir:
            store = TranscriptStore(Path(temp_dir) / "transcript.sqlite3")
            _runner, result, terminal = self._stream_final(
                deltas,
                text,
                transcript_store=store,
            )
            row = store.get_last(workspace_key="workspace", thread_id="thread")
            self.assertEqual(result.outcome, "COMPLETED")
            self.assertEqual(terminal.visible_bytes, len(text.encode("utf-8")))
            self.assertEqual(terminal.visible_digest.hexdigest(), hashlib.sha256(text.encode("utf-8")).hexdigest())
            self.assertEqual(row.retained_bytes, MAX_RESPONSE_BYTES)
            self.assertTrue(row.truncated)
            self.assertEqual(row.total_bytes, len(text.encode("utf-8")))
            self.assertIsNone(result.protocol_failure_reason)
            store.close()

    def test_20_mib_stream_has_bounded_working_state_and_identical_final(self) -> None:
        chunk = "z" * (1024 * 1024)
        deltas = [chunk] * 20
        authoritative = "".join(deltas)
        _runner, result, terminal = self._stream_final(deltas, authoritative)
        self.assertEqual(result.outcome, "COMPLETED")
        self.assertEqual(terminal.visible_bytes, 20 * 1024 * 1024)
        self.assertEqual(result.final_reconciliations[-1]["relationship"], "identical")
        self.assertLessEqual(result.canonical_stream_retained_bytes, MAX_RESPONSE_BYTES)
        self.assertTrue(result.canonical_stream_truncated)
        self.assertEqual(result.canonical_stream_bytes, 20 * 1024 * 1024)
        self.assertIs(result.agent_text, authoritative)
        self.assertFalse(hasattr(result, "_canonical_stream_spill"))
        self.assertFalse(hasattr(result, "_canonical_stream_hasher"))

    def test_large_authoritative_mismatch_reconciles_without_fake_equality(self) -> None:
        streamed = ("a" * MAX_RESPONSE_BYTES) + "x"
        authoritative = ("a" * MAX_RESPONSE_BYTES) + "y"
        _runner, result, terminal = self._stream_final(
            [streamed[:MAX_RESPONSE_BYTES], "x"],
            authoritative,
        )
        self.assertEqual(result.outcome, "COMPLETED")
        self.assertEqual(result.final_reconciliations[-1]["relationship"], "divergent")
        self.assertEqual(terminal.reconciled, [authoritative])
        self.assertEqual(result.agent_text, authoritative)
        self.assertIsNone(result.protocol_failure_reason)

    def test_multibyte_character_crossing_retention_boundary_is_exact(self) -> None:
        text = ("x" * (MAX_RESPONSE_BYTES - 1)) + "🙂" + "later"
        with tempfile.TemporaryDirectory(dir=_bootstrap.TEST_TEMP_ROOT) as temp_dir:
            store = TranscriptStore(Path(temp_dir) / "transcript.sqlite3")
            _runner, result, terminal = self._stream_final(
                [text[: MAX_RESPONSE_BYTES - 1], "🙂", "later"],
                text,
                transcript_store=store,
            )
            row = store.get_last(workspace_key="workspace", thread_id="thread")
            self.assertEqual(result.outcome, "COMPLETED")
            self.assertEqual(terminal.visible_bytes, len(text.encode("utf-8")))
            self.assertEqual(row.text, "x" * (MAX_RESPONSE_BYTES - 1))
            self.assertEqual(row.retained_bytes, MAX_RESPONSE_BYTES - 1)
            self.assertTrue(row.truncated)
            row.text.encode("utf-8").decode("utf-8")
            store.close()

    def test_cli_metadata_fast_paths_are_bounded_and_exact(self) -> None:
        script = ROOT / "runtime" / "cx2" / "cx2_cli.py"
        timings: list[float] = []
        for _ in range(5):
            start = time.perf_counter()
            completed = subprocess.run(
                [sys.executable, str(script), "--version"],
                cwd=ROOT, text=True, capture_output=True, timeout=5,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            timings.append((time.perf_counter() - start) * 1000)
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(
                completed.stdout.splitlines(),
                [f"CX2 CLI {CX2_VERSION}", f"CX2 runtime {CX2_VERSION}", "Router 1.2.2"],
            )
        self.assertLess(statistics.median(timings), 900)


class TestProcessAndCanary2014(unittest.TestCase):
    def test_process_tree_termination_returns_truthful_outcome(self) -> None:
        from bounded_verification_runner import ProcessTerminationOutcome, kill_process_tree

        with patch("bounded_verification_runner.subprocess.run") as run:
            run.return_value = MagicMock(returncode=5)
            outcome = kill_process_tree(12345)
        self.assertIsInstance(outcome, ProcessTerminationOutcome)
        self.assertFalse(outcome.verified)
        self.assertIn("taskkill", outcome.diagnostic.casefold())

    def test_runtime_canary_requires_explicit_disposable_root(self) -> None:
        source = (ROOT / "runtime" / "cx2" / "cx2_runtime_canary.py").read_text(encoding="utf-8")
        self.assertNotIn("Path.home() / \".cx\"", source)
        self.assertIn("DISPOSABLE_CX_HOME", source)
        self.assertIn("refusing production", source.casefold())

    def test_app_server_shutdown_records_surviving_process(self) -> None:
        client = AppServerClient(Path("missing"))
        process = MagicMock()
        process.pid = 12345
        process.wait.side_effect = [
            subprocess.TimeoutExpired("app-server", 2),
            subprocess.TimeoutExpired("app-server", 2),
            subprocess.TimeoutExpired("app-server", 2),
        ]
        process.poll.return_value = None
        client.process = process
        with patch("client.subprocess.run", return_value=MagicMock(returncode=5)), patch(
            "client.STDERR_FILE", ROOT / "does-not-write.log"
        ), patch.object(Path, "write_text", return_value=0):
            client.close()
        self.assertTrue(any("remained alive" in item for item in client.close_diagnostics))

    def test_installer_manifest_health_is_offline_and_exact(self) -> None:
        from installer_health import offline_install_health

        with tempfile.TemporaryDirectory(
            prefix="cx2-health-", dir=_bootstrap.TEST_TEMP_ROOT
        ) as directory:
            root = Path(directory)
            managed = root / "runtime" / "cx2" / "module.py"
            managed.parent.mkdir(parents=True)
            managed.write_text("value = 1\n", encoding="utf-8")
            manifest = {
                "schema": 1,
                "version": "2.0.14",
                "sha256": {
                    "runtime\\cx2\\module.py": hashlib.sha256(managed.read_bytes()).hexdigest()
                },
            }
            (managed.parent / "managed-files.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            healthy, detail = offline_install_health(root)
            self.assertTrue(healthy, detail)
            managed.write_text("tampered\n", encoding="utf-8")
            healthy, detail = offline_install_health(root)
            self.assertFalse(healthy)
            self.assertIn("mismatch", detail)


if __name__ == "__main__":
    unittest.main()
