from __future__ import annotations

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _bootstrap

from client import AppServerClient
from turn_runner import (
    FINAL_CANDIDATE_EVIDENCE_MAX_BYTES,
    MAX_FINAL_ANSWER_CANDIDATES,
    MAX_FINAL_CANDIDATE_ITEM_ID_BYTES,
    MAX_UNRESOLVED_PRESTART_ITEMS,
    StreamingTurnRunner,
    TurnRunResult,
    UNRESOLVED_ITEM_MAX_BYTES,
    UNRESOLVED_TURN_MAX_BYTES,
    _cx2_extract_raw_response_final_answer,
    _cx2_extract_thread_final_answer,
)
from terminal_ui import TerminalRenderer
from transcript_store import TranscriptStore


class NullClient:
    def request(self, method, params=None, timeout=15.0):
        return {}

    def respond(self, request_id, result):
        return None

    def respond_error(self, request_id, code, message):
        return None

    def drain_notifications(self):
        return []

    def drain_matching_notifications(self, predicate):
        return []

    def drain_server_requests(self):
        return []

    def drain_unknown(self):
        return []


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


class LifecycleClient(NullClient):
    def __init__(
        self,
        clock: FakeClock,
        notifications: list[tuple[float, dict]],
        *,
        thread_read: dict | None = None,
        start_status: str = "inProgress",
    ) -> None:
        self.clock = clock
        self.notifications = list(notifications)
        self.thread_read = thread_read
        self.start_status = start_status
        self.thread_read_calls = 0

    def request(self, method, params=None, timeout=15.0):
        if method == "turn/start":
            return {
                "turn": {
                    "id": "turn-1",
                    "status": self.start_status,
                }
            }
        if method == "thread/read":
            self.thread_read_calls += 1
            return self.thread_read or {}
        return {}

    def drain_notifications(self):
        ready = [value for at, value in self.notifications if at <= self.clock.now]
        self.notifications = [
            (at, value) for at, value in self.notifications if at > self.clock.now
        ]
        return ready

    def drain_matching_notifications(self, predicate):
        ready = self.drain_notifications()
        matched = []
        retained = []
        for value in ready:
            if predicate(value):
                matched.append(value)
            else:
                retained.append(value)
        self.notifications = [
            (self.clock.now, value) for value in retained
        ] + self.notifications
        return matched


class TTYStream(io.StringIO):
    def isatty(self):
        return True


def event(method: str, params: dict | None = None) -> dict:
    body = {"threadId": "thread-1", "turnId": "turn-1"}
    body.update(params or {})
    return {"method": method, "params": body}


def agent_item(item_id: str, phase=None, text: str = "") -> dict:
    return {
        "id": item_id,
        "type": "agentMessage",
        "text": text,
        "phase": phase,
    }


class TestFinalAnswerLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = StreamingTurnRunner(NullClient(), live=False)
        self.result = TurnRunResult(thread_id="thread-1", turn_id="turn-1")

    def handle(self, method: str, params: dict | None = None) -> None:
        self.runner._handle_notification(self.result, event(method, params))

    def start(self, item_id: str, phase=None) -> None:
        self.handle("item/started", {"item": agent_item(item_id, phase)})

    def delta(self, item_id: str, text: str, **identity) -> None:
        params = {"itemId": item_id, "delta": text}
        params.update(identity)
        self.handle("item/agentMessage/delta", params)

    def complete(self, item_id: str, phase=None, text: str = "") -> None:
        self.handle(
            "item/completed",
            {"item": agent_item(item_id, phase, text)},
        )

    def run_integrated(
        self,
        notifications: list[tuple[float, dict]],
        *,
        thread_read: dict | None = None,
        live: bool = False,
        renderer: TerminalRenderer | None = None,
        transcript_store: TranscriptStore | None = None,
    ) -> tuple[TurnRunResult, LifecycleClient]:
        clock = FakeClock()
        client = LifecycleClient(
            clock,
            notifications,
            thread_read=thread_read,
        )
        runner = StreamingTurnRunner(
            client,
            live=live,
            poll_interval=0.01,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            timeout_reconciliation_grace=0.05,
        )
        call = lambda: runner.run_turn(
            thread_id="thread-1",
            prompt="test",
            cwd=ROOT,
            model="test",
            effort="low",
            permissions="read-only",
            approval_policy="never",
            idle_timeout=1.0,
            hard_timeout=2.0,
            transcript_store=transcript_store,
            transcript_workspace_key="workspace" if transcript_store is not None else None,
            transcript_display_workspace="workspace" if transcript_store is not None else None,
        )
        if renderer is None:
            return call(), client
        with patch("turn_runner._CX2_TERMINAL", renderer):
            return call(), client

    def test_final_answer_started_before_delta_is_canonical(self) -> None:
        self.start("final", "final_answer")
        self.delta("final", "answer")
        self.assertEqual(self.result.agent_text, "answer")

    def test_commentary_and_null_started_before_delta_are_not_canonical(self) -> None:
        self.start("commentary", "commentary")
        self.delta("commentary", "progress")
        self.start("unknown", None)
        self.delta("unknown", "candidate")
        self.assertEqual(self.result.agent_text, "")
        state = self.result.agent_message_items[("thread-1", "turn-1", "unknown")]
        self.assertEqual(state.buffered_text, "candidate")

    def test_delta_before_started_is_buffered_then_confirmed(self) -> None:
        self.delta("late", "answer")
        self.assertEqual(self.result.agent_text, "")
        self.start("late", "final_answer")
        self.assertEqual(self.result.agent_text, "answer")

    def test_delta_then_null_phase_remains_bounded_and_noncanonical(self) -> None:
        self.delta("late", "candidate")
        self.start("late", None)
        self.complete("late", None, "candidate")
        state = self.result.agent_message_items[("thread-1", "turn-1", "late")]
        self.assertEqual(state.buffered_text, "candidate")
        self.assertEqual(self.result.agent_text, "")

    def test_foreign_or_malformed_delta_is_rejected(self) -> None:
        self.start("final", "final_answer")
        self.runner._handle_notification(
            self.result,
            event(
                "item/agentMessage/delta",
                {"itemId": "final", "delta": "x", "threadId": "foreign"},
            ),
        )
        self.runner._handle_notification(
            self.result,
            event(
                "item/agentMessage/delta",
                {"itemId": "final", "delta": "y", "turnId": "foreign"},
            ),
        )
        self.handle("item/agentMessage/delta", {"delta": "z"})
        self.assertEqual(self.result.agent_text, "")

    def test_canonical_events_require_exact_thread_and_turn_identity(self) -> None:
        final = agent_item("final", "final_answer", "answer")
        cases = (
            {"turnId": "turn-1", "item": final},
            {"threadId": "thread-1", "item": final},
            {"item": final},
            {"threadId": "wrong", "turnId": "turn-1", "item": final},
            {"threadId": "thread-1", "turnId": "wrong", "item": final},
            {"threadId": 1, "turnId": "turn-1", "item": final},
            {"threadId": "thread-1", "turnId": [], "item": final},
        )
        for params in cases:
            with self.subTest(params=params):
                isolated = TurnRunResult("thread-1", "turn-1")
                self.runner._handle_notification(
                    isolated,
                    {"method": "item/completed", "params": params},
                )
                self.assertFalse(isolated.authoritative_final_evidence)
                self.assertEqual(isolated.agent_text, "")
                self.assertEqual(isolated.identity_rejection_count, 1)

    def test_raw_final_missing_or_wrong_identity_is_rejected(self) -> None:
        raw = {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "answer"}],
        }
        for params in (
            {"turnId": "turn-1", "item": raw},
            {"threadId": "thread-1", "item": raw},
            {"threadId": "wrong", "turnId": "turn-1", "item": raw},
            {"threadId": "thread-1", "turnId": "wrong", "item": raw},
        ):
            with self.subTest(params=params):
                isolated = TurnRunResult("thread-1", "turn-1")
                self.runner._handle_notification(
                    isolated,
                    {"method": "rawResponseItem/completed", "params": params},
                )
                self.assertFalse(isolated.authoritative_final_evidence)

    def test_wrong_item_id_does_not_enter_known_final(self) -> None:
        self.start("final", "final_answer")
        self.delta("other", "not final")
        self.assertEqual(self.result.agent_text, "")

    def test_only_completed_final_answer_is_authoritative(self) -> None:
        self.complete("comment", "commentary", "commentary")
        self.complete("unknown", None, "unknown")
        self.assertEqual(self.result.agent_text, "")
        self.complete("final", "final_answer", "answer")
        self.assertEqual(self.result.agent_text, "answer")

    def test_multiple_agent_messages_do_not_mix_commentary_and_final(self) -> None:
        self.start("comment", "commentary")
        self.delta("comment", "thinking")
        self.start("final", "final_answer")
        self.delta("final", "answer")
        self.assertEqual(self.result.agent_text, "answer")

    def test_unresolved_item_boundary_and_overflow(self) -> None:
        exact = "x" * UNRESOLVED_ITEM_MAX_BYTES
        self.delta("unknown", exact)
        state = self.result.agent_message_items[("thread-1", "turn-1", "unknown")]
        self.assertEqual(state.buffered_bytes, UNRESOLVED_ITEM_MAX_BYTES)
        self.assertFalse(state.overflowed)
        self.delta("unknown", "y")
        self.assertTrue(state.overflowed)
        self.assertEqual(state.buffered_bytes, UNRESOLVED_ITEM_MAX_BYTES)
        self.assertEqual(state.dropped_bytes, 1)
        self.assertEqual(state.overflow_event_count, 1)
        self.assertEqual(self.result.unresolved_bytes_dropped, 1)
        self.assertEqual(self.result.overflow_event_count, 1)
        self.assertEqual(self.result.agent_text, "")
        self.assertTrue(any("overflow" in warning for warning in self.result.warnings))

    def test_unresolved_aggregate_boundary_and_overflow(self) -> None:
        chunk = "x" * UNRESOLVED_ITEM_MAX_BYTES
        for index in range(4):
            self.delta(f"item-{index}", chunk)
        self.assertEqual(self.result.unresolved_agent_bytes, UNRESOLVED_TURN_MAX_BYTES)
        self.assertFalse(any(state.overflowed for state in self.result.agent_message_items.values()))
        self.delta("item-4", "overflow")
        overflow = self.result.agent_message_items[("thread-1", "turn-1", "item-4")]
        self.assertTrue(overflow.overflowed)
        self.assertEqual(overflow.buffered_bytes, 0)
        self.assertEqual(self.result.unresolved_bytes_dropped, len("overflow"))
        self.assertEqual(self.result.agent_text, "")

    def test_unresolved_utf8_boundaries_and_repeated_overflow_are_exact(self) -> None:
        exact = "🙂" * (UNRESOLVED_ITEM_MAX_BYTES // 4)
        self.delta("emoji", exact)
        state = self.result.agent_message_items[("thread-1", "turn-1", "emoji")]
        self.assertEqual(state.buffered_bytes, UNRESOLVED_ITEM_MAX_BYTES)
        for delta in ("🙂", "ğ", "Türkçe"):
            self.delta("emoji", delta)
        expected_dropped = sum(len(value.encode("utf-8")) for value in ("🙂", "ğ", "Türkçe"))
        self.assertEqual(state.dropped_bytes, expected_dropped)
        self.assertEqual(state.overflow_event_count, 3)
        self.assertEqual(self.result.unresolved_bytes_dropped, expected_dropped)
        self.assertEqual(len(self.result.warnings), 1)

    def test_unresolved_prestart_registry_and_warning_metadata_are_bounded(self) -> None:
        for index in range(1000):
            self.delta(f"unknown-{index}", "🙂")
        self.assertEqual(
            len(self.result.agent_message_items),
            MAX_UNRESOLVED_PRESTART_ITEMS,
        )
        self.assertEqual(
            self.result.unresolved_prestart_items,
            MAX_UNRESOLVED_PRESTART_ITEMS,
        )
        self.assertEqual(
            self.result.unresolved_items_dropped,
            1000 - MAX_UNRESOLVED_PRESTART_ITEMS,
        )
        self.assertEqual(
            self.result.unresolved_bytes_dropped,
            (1000 - MAX_UNRESOLVED_PRESTART_ITEMS) * 4,
        )
        self.assertEqual(len(self.result.warnings), 1)

    def test_valid_started_agent_items_are_not_subject_to_prestart_cap(self) -> None:
        for index in range(MAX_UNRESOLVED_PRESTART_ITEMS + 50):
            self.start(f"known-{index}", "commentary")
        self.assertEqual(
            len(self.result.agent_message_items),
            MAX_UNRESOLVED_PRESTART_ITEMS + 50,
        )
        self.assertEqual(self.result.unresolved_prestart_items, 0)
        self.assertEqual(self.result.unresolved_items_dropped, 0)

    def test_extremely_long_unknown_item_id_is_not_retained(self) -> None:
        item_id = "x" * 100_000
        self.delta(item_id, "Türkçe🙂")
        self.assertEqual(self.result.agent_message_items, {})
        self.assertEqual(self.result.unresolved_items_dropped, 1)
        self.assertEqual(
            self.result.unresolved_bytes_dropped,
            len("Türkçe🙂".encode("utf-8")),
        )
        self.assertNotIn(item_id, repr(self.result.to_dict()))

    def _reconcile(self, streamed: str, completed: str) -> str:
        self.start("final", "final_answer")
        if streamed:
            self.delta("final", streamed)
        self.complete("final", "final_answer", completed)
        return self.result.final_reconciliations[-1]["relationship"]

    def test_reconciliation_identical(self) -> None:
        self.assertEqual(self._reconcile("answer", "answer"), "identical")
        self.assertEqual(self.result.agent_text, "answer")

    def test_reconciliation_streamed_prefix(self) -> None:
        self.assertEqual(self._reconcile("ans", "answer"), "streamed_prefix")
        self.assertEqual(self.result.agent_text, "answer")

    def test_reconciliation_completed_prefix(self) -> None:
        self.assertEqual(self._reconcile("answer extra", "answer"), "completed_prefix")
        self.assertEqual(self.result.agent_text, "answer")

    def test_reconciliation_divergent_uses_completed(self) -> None:
        self.assertEqual(self._reconcile("alpha", "beta"), "divergent")
        self.assertEqual(self.result.agent_text, "beta")

    def test_reconciliation_missing_streamed(self) -> None:
        self.assertEqual(self._reconcile("", "answer"), "missing_streamed")
        self.assertEqual(self.result.agent_text, "answer")

    def test_explicit_empty_completion_is_authoritative(self) -> None:
        self.assertEqual(self._reconcile("answer", ""), "completed_prefix")
        self.assertEqual(self.result.agent_text, "")
        self.assertTrue(self.result.authoritative_final_evidence)
        self.assertTrue(self.result.canonical_final_reconciled)

    def test_multiple_different_finals_are_order_independent_failure(self) -> None:
        snapshots = []
        for first, second in (("A", "B"), ("B", "A")):
            isolated = TurnRunResult("thread-1", "turn-1")
            for item_id, text in ((first, first), (second, second)):
                self.runner._handle_notification(
                    isolated,
                    event(
                        "item/completed",
                        {"item": agent_item(item_id, "final_answer", text)},
                    ),
                )
            snapshots.append(
                (
                    isolated.agent_text,
                    isolated.final_ambiguity_reason,
                    isolated.canonical_final_reconciled,
                    isolated.final_candidate_count,
                )
            )
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(
            snapshots[0],
            ("", "MULTIPLE_FINAL_ANSWER_AMBIGUOUS", False, 2),
        )

    def test_final_candidate_exact_boundary_is_retained(self) -> None:
        text = "x" * (FINAL_CANDIDATE_EVIDENCE_MAX_BYTES + 100)
        for index in range(MAX_FINAL_ANSWER_CANDIDATES):
            self.complete(f"final-{index}", "final_answer", text)

        self.assertEqual(
            self.result.final_candidates_retained,
            MAX_FINAL_ANSWER_CANDIDATES,
        )
        self.assertEqual(self.result.final_candidates_dropped, 0)
        self.assertEqual(self.result.final_candidate_overflow_events, 0)
        self.assertEqual(
            len(self.result.agent_message_items),
            MAX_FINAL_ANSWER_CANDIDATES,
        )
        for state in self.result.agent_message_items.values():
            self.assertLessEqual(
                len((state.completed_text or "").encode("utf-8")),
                FINAL_CANDIDATE_EVIDENCE_MAX_BYTES,
            )
            self.assertLessEqual(
                len(state.streamed_text.encode("utf-8")),
                FINAL_CANDIDATE_EVIDENCE_MAX_BYTES,
            )
        for summary in self.result.completed_items:
            self.assertNotIn("text", summary)
            self.assertNotIn("message", summary)

    def test_streamed_candidate_evidence_is_bounded_without_truncating_answer(self) -> None:
        text = "Türkçe🙂" * 2000
        self.start("final", "final_answer")
        self.delta("final", text)
        state = self.result.agent_message_items[("thread-1", "turn-1", "final")]
        self.assertEqual(self.result.agent_text, text)
        self.assertLessEqual(
            len(state.streamed_text.encode("utf-8")),
            FINAL_CANDIDATE_EVIDENCE_MAX_BYTES,
        )
        self.complete("final", "final_answer", text)
        self.assertEqual(self.result.agent_text, text)
        self.assertEqual(state.streamed_text, "")
        self.assertLessEqual(
            len((state.completed_text or "").encode("utf-8")),
            FINAL_CANDIDATE_EVIDENCE_MAX_BYTES,
        )

    def test_seventeenth_final_candidate_fails_closed_without_retention(self) -> None:
        for index in range(MAX_FINAL_ANSWER_CANDIDATES + 1):
            self.complete(f"final-{index}", "final_answer", "same")

        self.assertEqual(
            self.result.final_candidates_retained,
            MAX_FINAL_ANSWER_CANDIDATES,
        )
        self.assertEqual(self.result.final_candidates_dropped, 1)
        self.assertEqual(self.result.final_candidate_overflow_events, 1)
        self.assertEqual(
            len(self.result.agent_message_items),
            MAX_FINAL_ANSWER_CANDIDATES,
        )
        self.assertEqual(
            self.result.final_ambiguity_reason,
            "FINAL_ANSWER_CANDIDATE_LIMIT_EXCEEDED",
        )
        self.assertEqual(len(self.result.completed_items), MAX_FINAL_ANSWER_CANDIDATES)

    def test_one_thousand_final_candidates_leave_bounded_state(self) -> None:
        for index in range(1000):
            self.complete(f"final-{index}", "final_answer", "same")

        self.assertEqual(
            self.result.final_candidates_retained,
            MAX_FINAL_ANSWER_CANDIDATES,
        )
        self.assertEqual(
            self.result.final_candidates_dropped,
            1000 - MAX_FINAL_ANSWER_CANDIDATES,
        )
        self.assertEqual(
            self.result.final_candidate_overflow_events,
            1000 - MAX_FINAL_ANSWER_CANDIDATES,
        )
        self.assertEqual(
            len(self.result.agent_message_items),
            MAX_FINAL_ANSWER_CANDIDATES,
        )
        self.assertEqual(len(self.result.completed_items), MAX_FINAL_ANSWER_CANDIDATES)
        self.assertLessEqual(len(self.result.final_reconciliations), 64)
        self.assertLessEqual(len(self.result.warnings), 2)

        self.result.status = "completed"
        self.runner._finalize_terminal_result(self.result, allow_recovery=False)
        self.assertEqual(self.result.status, "failed")

    def test_rejected_started_candidates_cannot_reenter_as_unresolved_deltas(self) -> None:
        for index in range(1000):
            item_id = f"final-{index}"
            self.start(item_id, "final_answer")
            self.delta(item_id, "x")

        self.assertEqual(
            len(self.result.agent_message_items),
            MAX_FINAL_ANSWER_CANDIDATES,
        )
        self.assertEqual(
            self.result.final_candidates_retained,
            MAX_FINAL_ANSWER_CANDIDATES,
        )
        self.assertEqual(
            self.result.final_candidates_dropped,
            1000 - MAX_FINAL_ANSWER_CANDIDATES,
        )
        self.assertEqual(self.result.unresolved_prestart_items, 0)
        self.assertEqual(
            self.result.unresolved_items_dropped,
            1000 - MAX_FINAL_ANSWER_CANDIDATES,
        )

    def test_final_candidate_item_id_byte_bound(self) -> None:
        exact = "ğ" * (MAX_FINAL_CANDIDATE_ITEM_ID_BYTES // 2)
        too_large = exact + "a"
        huge = "x" * 100_000

        self.complete(exact, "final_answer", "accepted")
        self.complete(too_large, "final_answer", "rejected")
        self.complete(huge, "final_answer", "rejected")
        for index in range(1000):
            self.complete(f"{index:04d}-" + ("z" * 1000), "final_answer", "rejected")

        self.assertEqual(self.result.final_candidates_retained, 1)
        self.assertEqual(self.result.final_candidates_dropped, 1002)
        self.assertEqual(self.result.final_candidate_overflow_events, 1002)
        self.assertEqual(len(self.result.agent_message_items), 1)
        self.assertEqual(len(self.result.completed_items), 1)
        self.assertLessEqual(
            len(self.result.final_candidate_rejection_diagnostic or ""),
            64,
        )
        serialized = repr(self.result.to_dict())
        self.assertNotIn(too_large, serialized)
        self.assertNotIn(huge, serialized)

    def test_identical_duplicate_finals_are_used_once(self) -> None:
        self.complete("B", "final_answer", "same")
        self.complete("A", "final_answer", "same")
        self.assertEqual(self.result.agent_text, "same")
        self.assertEqual(self.result.canonical_final_item_id, "A")
        self.assertEqual(self.result.final_candidate_count, 2)
        self.assertEqual(self.result.duplicate_final_count, 1)
        self.assertIsNone(self.result.final_ambiguity_reason)

    def test_identical_duplicate_item_finals_are_order_independent(self) -> None:
        snapshots = []
        for order in (("A", "B"), ("B", "A")):
            isolated = TurnRunResult("thread-1", "turn-1")
            for item_id in order:
                self.runner._handle_notification(
                    isolated,
                    event(
                        "item/completed",
                        {"item": agent_item(item_id, "final_answer", "same")},
                    ),
                )
            snapshots.append(
                (
                    isolated.agent_text,
                    isolated.canonical_final_item_id,
                    isolated.duplicate_final_count,
                    isolated.final_ambiguity_reason,
                )
            )
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[0], ("same", "A", 1, None))

    def test_identical_raw_finals_are_duplicate_equivalent(self) -> None:
        raw = {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "same"}],
        }
        self.handle("rawResponseItem/completed", {"item": raw})
        self.handle("rawResponseItem/completed", {"item": raw})
        self.assertEqual(self.result.agent_text, "same")
        self.assertEqual(self.result.raw_final_duplicate_count, 1)
        self.assertFalse(self.result.raw_final_conflict)
        self.assertIsNone(self.result.final_ambiguity_reason)

    def test_different_raw_finals_are_order_independent_failure(self) -> None:
        snapshots = []
        for order in (("A", "B"), ("B", "A")):
            isolated = TurnRunResult("thread-1", "turn-1")
            for text in order:
                raw = {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": text}],
                }
                self.runner._handle_notification(
                    isolated,
                    event("rawResponseItem/completed", {"item": raw}),
                )
            isolated.status = "completed"
            self.runner._finalize_terminal_result(isolated, allow_recovery=False)
            snapshots.append(
                (
                    isolated.status,
                    isolated.agent_text,
                    isolated.final_ambiguity_reason,
                    isolated.raw_final_conflict,
                    isolated.raw_final_digest,
                    isolated.raw_final_conflict_digest,
                )
            )
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[0][0], "failed")
        self.assertEqual(
            snapshots[0][2],
            "MULTIPLE_RAW_FINAL_ANSWER_AMBIGUOUS",
        )

    def test_item_and_raw_final_source_precedence_is_order_independent(self) -> None:
        def snapshot(order, raw_text):
            isolated = TurnRunResult("thread-1", "turn-1")
            for source in order:
                if source == "item":
                    notification = event(
                        "item/completed",
                        {"item": agent_item("final", "final_answer", "item")},
                    )
                else:
                    notification = event(
                        "rawResponseItem/completed",
                        {
                            "item": {
                                "type": "message",
                                "role": "assistant",
                                "phase": "final_answer",
                                "content": [{"type": "output_text", "text": raw_text}],
                            }
                        },
                    )
                self.runner._handle_notification(isolated, notification)
            return (
                isolated.agent_text,
                isolated.canonical_final_source,
                isolated.final_ambiguity_reason,
            )

        for raw_text in ("item", "raw-conflict"):
            with self.subTest(raw_text=raw_text):
                self.assertEqual(
                    snapshot(("item", "raw"), raw_text),
                    snapshot(("raw", "item"), raw_text),
                )
                self.assertEqual(
                    snapshot(("item", "raw"), raw_text),
                    ("item", "item/completed", None),
                )

    def test_completion_source_precedence_is_deterministic(self) -> None:
        self.complete("final", "final_answer", "item answer")
        raw = {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "raw answer"}],
        }
        self.handle("rawResponseItem/completed", {"item": raw})
        self.handle(
            "turn/completed",
            {
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [agent_item("final", "final_answer", "turn answer")],
                }
            },
        )
        self.assertEqual(self.result.agent_text, "item answer")
        self.assertEqual(self.result.canonical_final_source, "item/completed")

    def test_turn_payload_recovers_only_final_answer_for_exact_turn(self) -> None:
        turn = {
            "id": "turn-1",
            "status": "completed",
            "items": [
                agent_item("comment", "commentary", "progress"),
                agent_item("final", "final_answer", "answer"),
            ],
        }
        self.handle("turn/completed", {"turn": turn})
        self.assertEqual(self.result.agent_text, "answer")

        other = TurnRunResult(thread_id="thread-1", turn_id="turn-1")
        self.runner._handle_notification(
            other,
            event("turn/completed", {"turn": {**turn, "id": "wrong-turn"}}),
        )
        self.assertEqual(other.agent_text, "")
        self.assertEqual(other.status, "inProgress")

        missing_thread = TurnRunResult(thread_id="thread-1", turn_id="turn-1")
        self.runner._handle_notification(
            missing_thread,
            {
                "method": "turn/completed",
                "params": {"turn": turn},
            },
        )
        self.assertEqual(missing_thread.status, "inProgress")
        self.assertEqual(missing_thread.identity_rejection_count, 1)

    def test_raw_response_final_accepted_and_commentary_rejected(self) -> None:
        final = {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "answer"}],
        }
        self.assertEqual(_cx2_extract_raw_response_final_answer(final), "answer")
        self.handle("rawResponseItem/completed", {"item": final})
        self.assertEqual(self.result.agent_text, "answer")
        commentary = {**final, "phase": "commentary"}
        self.assertIsNone(_cx2_extract_raw_response_final_answer(commentary))

    def test_thread_read_is_exact_turn_and_strict_final_phase(self) -> None:
        payload = {
            "thread": {
                "id": "thread-1",
                "turns": [
                    {"id": "wrong", "items": [agent_item("x", "final_answer", "wrong")]},
                    {
                        "id": "turn-1",
                        "items": [
                            agent_item("c", "commentary", "progress"),
                            agent_item("f", "final_answer", "answer"),
                        ],
                    },
                ]
            }
        }
        self.assertEqual(
            _cx2_extract_thread_final_answer(payload, expected_turn_id="turn-1"),
            "answer",
        )
        self.assertIsNone(
            _cx2_extract_thread_final_answer(payload, expected_turn_id="missing")
        )
        self.assertIsNone(
            _cx2_extract_thread_final_answer(
                payload,
                expected_turn_id="turn-1",
                expected_thread_id="wrong-thread",
            )
        )

    def test_integrated_stream_without_completion_uses_strict_thread_recovery(self) -> None:
        notifications = [
            (0.0, event("item/started", {"item": agent_item("f", "final_answer")})),
            (0.0, event("item/agentMessage/delta", {"itemId": "f", "delta": "stream"})),
            (
                0.01,
                event(
                    "turn/completed",
                    {"turn": {"id": "turn-1", "status": "completed"}},
                ),
            ),
        ]
        recovery = {
            "thread": {
                "id": "thread-1",
                "turns": [
                    {
                        "id": "turn-1",
                        "items": [agent_item("f", "final_answer", "stream recovered")],
                    }
                ],
            }
        }
        result, client = self.run_integrated(notifications, thread_read=recovery)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.agent_text, "stream recovered")
        self.assertTrue(result.confirmed_streamed_final)
        self.assertTrue(result.authoritative_final_evidence)
        self.assertGreaterEqual(client.thread_read_calls, 1)

    def test_integrated_late_final_after_terminal_is_reconciled(self) -> None:
        notifications = [
            (
                0.01,
                event(
                    "turn/completed",
                    {"turn": {"id": "turn-1", "status": "completed"}},
                ),
            ),
            (
                0.02,
                event(
                    "item/completed",
                    {"item": agent_item("f", "final_answer", "late")},
                ),
            ),
            (
                0.02,
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "other",
                        "turnId": "other",
                        "item": agent_item("x", "final_answer", "foreign"),
                    },
                },
            ),
        ]
        result, client = self.run_integrated(notifications)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.agent_text, "late")
        self.assertEqual(client.thread_read_calls, 0)
        self.assertNotIn("foreign", repr(result.to_dict()))

    def test_integrated_divergence_is_visibly_reconciled_in_tty(self) -> None:
        stream = TTYStream()
        renderer = TerminalRenderer(stream=stream)
        notifications = [
            (0.0, event("item/started", {"item": agent_item("f", "final_answer")})),
            (0.0, event("item/agentMessage/delta", {"itemId": "f", "delta": "STREAMED"})),
            (0.01, event("item/completed", {"item": agent_item("f", "final_answer", "CANONICAL")})),
            (0.02, event("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}})),
        ]
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            result, _client = self.run_integrated(
                notifications,
                live=True,
                renderer=renderer,
            )
        output = stream.getvalue()
        self.assertEqual(result.agent_text, "CANONICAL")
        self.assertIn("RESPONSE RECONCILED", output)
        self.assertIn("◆ CODEX RESPONSE · RECONCILED", output)
        self.assertIn("CANONICAL", output)
        self.assertIn("✓ Completed", output)

    def test_integrated_divergence_persists_only_authoritative_final_after_reopen(self) -> None:
        notifications = [
            (0.0, event("item/started", {"item": agent_item("f", "final_answer")})),
            (0.0, event("item/agentMessage/delta", {"itemId": "f", "delta": "STALE STREAM"})),
            (0.01, event("item/completed", {"item": agent_item("f", "final_answer", "CANONICAL FINAL")})),
            (0.02, event("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}})),
        ]
        with tempfile.TemporaryDirectory(dir=_bootstrap.TEST_TEMP_ROOT) as temp_dir:
            path = Path(temp_dir) / "transcript.sqlite3"
            store = TranscriptStore(path)
            result, _client = self.run_integrated(notifications, transcript_store=store)
            self.assertEqual(result.agent_text, "CANONICAL FINAL")
            store.close()
            reopened = TranscriptStore(path)
            row = reopened.get_last(workspace_key="workspace", thread_id="thread-1")
            self.assertIsNotNone(row)
            self.assertEqual(row.text, "CANONICAL FINAL")
            self.assertNotIn("STALE STREAM", row.text)
            self.assertEqual(row.authoritative_source, "item/completed")
            reopened.close()

    def test_integrated_completed_prefix_is_visibly_reconciled_non_tty(self) -> None:
        stream = io.StringIO()
        renderer = TerminalRenderer(stream=stream)
        notifications = [
            (0.0, event("item/started", {"item": agent_item("f", "final_answer")})),
            (0.0, event("item/agentMessage/delta", {"itemId": "f", "delta": "answer extra"})),
            (0.01, event("item/completed", {"item": agent_item("f", "final_answer", "answer")})),
            (0.02, event("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}})),
        ]
        result, _client = self.run_integrated(
            notifications,
            live=True,
            renderer=renderer,
        )
        output = stream.getvalue()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.agent_text, "answer")
        self.assertIn("RESPONSE RECONCILED", output)
        self.assertIn("◆ CODEX RESPONSE · RECONCILED\nanswer\n", output)

    def test_integrated_multiple_final_answers_fail_without_success_footer(self) -> None:
        stream = TTYStream()
        renderer = TerminalRenderer(stream=stream)
        notifications = [
            (0.0, event("item/completed", {"item": agent_item("A", "final_answer", "one")})),
            (0.0, event("item/completed", {"item": agent_item("B", "final_answer", "two")})),
            (0.01, event("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}})),
        ]
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            result, _client = self.run_integrated(
                notifications,
                live=True,
                renderer=renderer,
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.agent_text, "")
        self.assertEqual(
            result.final_ambiguity_reason,
            "MULTIPLE_FINAL_ANSWER_AMBIGUOUS",
        )
        self.assertNotIn("✓ Completed", stream.getvalue())

    def test_integrated_identical_duplicate_final_is_rendered_once(self) -> None:
        stream = TTYStream()
        renderer = TerminalRenderer(stream=stream)
        notifications = [
            (0.0, event("item/completed", {"item": agent_item("B", "final_answer", "SAME_TEXT")})),
            (0.0, event("item/completed", {"item": agent_item("A", "final_answer", "SAME_TEXT")})),
            (0.01, event("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}})),
        ]
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            result, _client = self.run_integrated(
                notifications,
                live=True,
                renderer=renderer,
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.canonical_final_item_id, "A")
        self.assertEqual(stream.getvalue().count("SAME_TEXT"), 1)
        self.assertEqual(stream.getvalue().count("✓ Completed"), 1)

    def test_integrated_missing_identity_cannot_authorize_success(self) -> None:
        invalid = {
            "method": "item/completed",
            "params": {"item": agent_item("f", "final_answer", "no ids")},
        }
        notifications = [
            (0.0, invalid),
            (0.01, event("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}})),
        ]
        result, _client = self.run_integrated(notifications)
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.authoritative_final_evidence)
        self.assertEqual(result.identity_rejection_count, 1)

    def test_integrated_stream_with_unavailable_recovery_is_partial_failure(self) -> None:
        notifications = [
            (0.0, event("item/started", {"item": agent_item("f", "final_answer")})),
            (0.0, event("item/agentMessage/delta", {"itemId": "f", "delta": "partial"})),
            (0.01, event("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}})),
        ]
        result, client = self.run_integrated(notifications)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.agent_text, "partial")
        self.assertFalse(result.authoritative_final_evidence)
        self.assertEqual(result.protocol_failure_reason, "MISSING_AUTHORITATIVE_FINAL")
        self.assertEqual(client.thread_read_calls, 10)

    def test_failure_and_interrupt_precedence_survive_late_final(self) -> None:
        for terminal_status in ("failed", "interrupted"):
            with self.subTest(status=terminal_status):
                stream = TTYStream()
                renderer = TerminalRenderer(stream=stream)
                notifications = [
                    (0.01, event("turn/completed", {"turn": {"id": "turn-1", "status": terminal_status}})),
                    (0.02, event("item/completed", {"item": agent_item("f", "final_answer", "late")})),
                ]
                with patch.dict("os.environ", {"NO_COLOR": "1"}):
                    result, _client = self.run_integrated(
                        notifications,
                        live=True,
                        renderer=renderer,
                    )
                self.assertEqual(result.status, terminal_status)
                self.assertEqual(result.agent_text, "late")
                self.assertNotIn("✓ Completed", stream.getvalue())

    def test_integrated_empty_final_is_valid_but_absent_final_is_not(self) -> None:
        stream = TTYStream()
        renderer = TerminalRenderer(stream=stream)
        empty_notifications = [
            (0.0, event("item/completed", {"item": agent_item("f", "final_answer", "")})),
            (0.01, event("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}})),
        ]
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            empty, _client = self.run_integrated(
                empty_notifications,
                live=True,
                renderer=renderer,
            )
        self.assertEqual(empty.status, "completed")
        self.assertEqual(empty.agent_text, "")
        self.assertIn("◆ CODEX RESPONSE", stream.getvalue())
        self.assertIn("✓ Completed", stream.getvalue())
        self.assertIn("0 lines", stream.getvalue())

        absent, _client = self.run_integrated(
            [(0.01, event("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}))]
        )
        self.assertEqual(absent.status, "failed")
        self.assertFalse(absent.authoritative_final_evidence)

    def test_terminal_turn_start_evidence_has_accurate_source_label(self) -> None:
        class DirectCompletedClient(NullClient):
            def request(self, method, params=None, timeout=15.0):
                if method == "turn/start":
                    return {
                        "turn": {
                            "id": "turn-1",
                            "status": "completed",
                            "items": [agent_item("f", "final_answer", "direct")],
                        }
                    }
                return {}

        runner = StreamingTurnRunner(
            DirectCompletedClient(),
            live=False,
            timeout_reconciliation_grace=0.0,
        )
        result = runner.run_turn(
            thread_id="thread-1",
            prompt="test",
            cwd=ROOT,
            model="test",
            effort="low",
            permissions="read-only",
            approval_policy="never",
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.canonical_final_source, "turn/start")
        self.assertEqual(result.final_reconciliations[-1]["source"], "turn/start")

    def test_reasoning_never_enters_registry_or_canonical_text(self) -> None:
        secret = "private reasoning"
        self.handle("item/reasoning/textDelta", {"delta": secret})
        self.assertEqual(self.result.agent_text, "")
        self.assertEqual(self.result.agent_message_items, {})
        self.assertNotIn(secret, repr(self.result.to_dict()))


class TestNotificationQueuePreservation(unittest.TestCase):
    @staticmethod
    def notification(label: str, *, thread_id="thread-1", turn_id="turn-2"):
        return {
            "method": label,
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
            },
        }

    @staticmethod
    def client() -> AppServerClient:
        return AppServerClient(Path("synthetic-codex.exe"))

    def test_selective_drain_preserves_fifo_for_all_queue_positions(self) -> None:
        current = self.notification("CURRENT_FINAL", turn_id="turn-1")
        future_a = self.notification("FUTURE_A")
        future_b = self.notification("FUTURE_B")

        for queued in (
            (current, future_a, future_b),
            (future_a, current, future_b),
        ):
            with self.subTest(order=[value["method"] for value in queued]):
                client = self.client()
                for value in queued:
                    client._route_message(value)
                matched = client.drain_matching_notifications(
                    lambda value: value["method"] == "CURRENT_FINAL"
                )
                self.assertEqual(matched, [current])
                self.assertEqual(client.drain_notifications(), [future_a, future_b])

    def test_arrival_during_selective_drain_follows_existing_backlog(self) -> None:
        client = self.client()
        future_a = self.notification("FUTURE_A")
        future_b = self.notification("FUTURE_B")
        client._route_message(future_a)

        def predicate(value):
            self.assertIs(value, future_a)
            client._route_message(future_b)
            return False

        self.assertEqual(client.drain_matching_notifications(predicate), [])
        self.assertEqual(client.drain_notifications(), [future_a, future_b])

    def test_runner_drain_preserves_future_wrong_thread_and_no_match(self) -> None:
        current = event(
            "item/completed",
            {"item": agent_item("final", "final_answer", "current")},
        )
        future_same_thread = event(
            "item/completed",
            {
                "turnId": "turn-2",
                "item": agent_item("future", "final_answer", "future"),
            },
        )
        wrong_thread = event(
            "item/completed",
            {
                "threadId": "other-thread",
                "turnId": "turn-1",
                "item": agent_item("foreign", "final_answer", "foreign"),
            },
        )

        for queued, expected in (
            (
                (current, future_same_thread, wrong_thread),
                [future_same_thread, wrong_thread],
            ),
            (
                (future_same_thread, current, wrong_thread),
                [future_same_thread, wrong_thread],
            ),
            (
                (future_same_thread, wrong_thread),
                [future_same_thread, wrong_thread],
            ),
        ):
            with self.subTest(order=[value["params"]["turnId"] for value in queued]):
                client = self.client()
                for value in queued:
                    client._route_message(value)
                runner = StreamingTurnRunner(
                    client,
                    live=False,
                    timeout_reconciliation_grace=0.0,
                )
                result = TurnRunResult("thread-1", "turn-1")
                runner._drain_late_final_events(result)
                self.assertEqual(client.drain_notifications(), expected)
                if current in queued:
                    self.assertEqual(result.agent_text, "current")
                else:
                    self.assertEqual(result.agent_text, "")

    def test_timeout_outcomes_survive_matching_late_final(self) -> None:
        late_final = event(
            "item/completed",
            {"item": agent_item("final", "final_answer", "late")},
        )
        for kind, expected in (("idle", "IDLE_TIMEOUT"), ("hard", "HARD_TIMEOUT")):
            with self.subTest(kind=kind):
                client = self.client()
                client._route_message(late_final)
                runner = StreamingTurnRunner(
                    client,
                    live=False,
                    timeout_reconciliation_grace=0.0,
                )
                result = TurnRunResult("thread-1", "turn-1", status="failed")
                result.timeout_diagnostics = {"timeout_kind": kind}
                runner._finalize_terminal_result(result)
                self.assertEqual(result.agent_text, "late")
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.outcome, expected)


class TestSemanticOutcome(unittest.TestCase):
    def test_result_outcomes_remain_distinct(self) -> None:
        expected = {
            "completed": "COMPLETED",
            "blocked": "BLOCKED",
            "failed": "FAILED",
            "interrupted": "INTERRUPTED",
        }
        for status, outcome in expected.items():
            with self.subTest(status=status):
                result = TurnRunResult("thread", "turn", status=status)
                self.assertEqual(result.outcome, outcome)

        idle = TurnRunResult("thread", "turn", status="failed")
        idle.timeout_diagnostics = {"timeout_kind": "idle"}
        self.assertEqual(idle.outcome, "IDLE_TIMEOUT")
        hard = TurnRunResult("thread", "turn", status="failed")
        hard.timeout_diagnostics = {"timeout_kind": "hard"}
        self.assertEqual(hard.outcome, "HARD_TIMEOUT")
        unknown = TurnRunResult("thread", "turn", status="inProgress")
        self.assertEqual(unknown.outcome, "PROCESS_OR_PROTOCOL_FAILURE")


if __name__ == "__main__":
    unittest.main()
