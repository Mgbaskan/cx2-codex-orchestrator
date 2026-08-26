from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
import _bootstrap

from turn_runner import StreamingTurnRunner, TurnRunResult, TurnTimeoutError


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


class ScheduledClient:
    def __init__(
        self,
        clock: FakeClock,
        notifications: list[tuple[float, dict]] | None = None,
        server_requests: list[tuple[float, dict]] | None = None,
    ) -> None:
        self.clock = clock
        self.notifications = list(notifications or [])
        self.server_requests = list(server_requests or [])
        self.interrupt_count = 0

    def _due(self, queue: list[tuple[float, dict]]) -> list[dict]:
        ready = [event for at, event in queue if at <= self.clock.now]
        queue[:] = [(at, event) for at, event in queue if at > self.clock.now]
        return ready

    def drain_notifications(self) -> list[dict]:
        return self._due(self.notifications)

    def drain_server_requests(self) -> list[dict]:
        return self._due(self.server_requests)

    def drain_unknown(self) -> list[dict]:
        return []

    def request(self, method: str, params=None, timeout: float = 15.0):
        if method == "turn/interrupt":
            self.interrupt_count += 1
        return {}

    def respond(self, request_id, result) -> None:
        return None

    def respond_error(self, request_id, code, message) -> None:
        return None


class InterruptCompletingClient(ScheduledClient):
    def request(self, method: str, params=None, timeout: float = 15.0):
        response = super().request(method, params, timeout)
        if method == "turn/interrupt":
            self.notifications.append(completed(self.clock.now + 0.1, "interrupted"))
        return response


class BoundaryCompletionClient(ScheduledClient):
    def __init__(self, clock: FakeClock, completion: dict) -> None:
        super().__init__(clock)
        self.completion = completion
        self.boundary_drains = 0

    def drain_notifications(self) -> list[dict]:
        if self.clock.now >= 5.0:
            self.boundary_drains += 1
            if self.boundary_drains == 2:
                return [self.completion]
        return []


def event(method: str, params: dict | None = None) -> dict:
    payload = {"threadId": "thread-1", "turnId": "turn-1"}
    payload.update(params or {})
    return {"method": method, "params": payload}


def completed(at: float, status: str = "completed") -> tuple[float, dict]:
    return (
        at,
        event(
            "turn/completed",
            {"turn": {"id": "turn-1", "status": status}},
        ),
    )


class TestProgressAwareTurnTimeout(unittest.TestCase):
    def make_result(self) -> TurnRunResult:
        return TurnRunResult(thread_id="thread-1", turn_id="turn-1")

    def run_schedule(
        self,
        notifications: list[tuple[float, dict]],
        *,
        idle: float,
        hard: float,
        client_type=ScheduledClient,
    ):
        clock = FakeClock()
        client = client_type(clock, notifications)
        runner = StreamingTurnRunner(
            client,
            live=False,
            poll_interval=1.0,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )
        result = self.make_result()
        return runner, client, result, runner.wait_for_turn(
            result,
            idle_timeout=idle,
            hard_timeout=hard,
        )

    def test_active_progress_extends_idle_past_old_absolute_limit(self) -> None:
        schedule = [
            (200.0, event("item/agentMessage/delta", {"itemId": "a", "delta": "a"})),
            (400.0, event("item/agentMessage/delta", {"itemId": "a", "delta": "b"})),
            (600.0, event("item/commandExecution/outputDelta", {"itemId": "c", "delta": "ok"})),
            completed(700.0),
        ]
        _runner, client, _result, final = self.run_schedule(
            schedule, idle=300.0, hard=3600.0
        )
        self.assertEqual(final.status, "completed")
        self.assertEqual(client.interrupt_count, 0)

    def test_true_idle_timeout_is_typed_and_interrupts_once(self) -> None:
        clock = FakeClock()
        client = ScheduledClient(clock)
        result = self.make_result()
        runner = StreamingTurnRunner(
            client,
            live=False,
            poll_interval=1.0,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )
        with self.assertRaises(TurnTimeoutError) as caught:
            runner.wait_for_turn(result, idle_timeout=5.0, hard_timeout=20.0)
        self.assertEqual(caught.exception.kind, "idle")
        self.assertEqual(client.interrupt_count, 1)
        self.assertEqual(result.timeout_diagnostics["timeout_kind"], "idle")

    def test_hard_timeout_wins_despite_continuous_progress(self) -> None:
        clock = FakeClock()
        schedule = [
            (at, event("item/agentMessage/delta", {"itemId": "a", "delta": "x"}))
            for at in (2.0, 4.0, 6.0, 8.0, 10.0)
        ]
        client = ScheduledClient(clock, schedule)
        runner = StreamingTurnRunner(
            client,
            live=False,
            poll_interval=1.0,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )
        with self.assertRaises(TurnTimeoutError) as caught:
            runner.wait_for_turn(self.make_result(), idle_timeout=3.0, hard_timeout=8.0)
        self.assertEqual(caught.exception.kind, "hard")
        self.assertEqual(client.interrupt_count, 1)

    def test_unrelated_events_do_not_extend_idle(self) -> None:
        unrelated = {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "other", "turnId": "other", "delta": "noise"},
        }
        clock = FakeClock()
        client = ScheduledClient(clock, [(1.0, unrelated), (2.0, unrelated), (3.0, unrelated)])
        runner = StreamingTurnRunner(client, live=False, poll_interval=1.0, monotonic=clock.monotonic, sleeper=clock.sleep)
        with self.assertRaises(TurnTimeoutError) as caught:
            runner.wait_for_turn(self.make_result(), idle_timeout=3.0, hard_timeout=20.0)
        self.assertEqual(caught.exception.kind, "idle")

    def test_token_telemetry_alone_does_not_extend_idle(self) -> None:
        telemetry = event("thread/tokenUsage/updated", {"tokenUsage": {"total": 1}})
        clock = FakeClock()
        client = ScheduledClient(clock, [(1.0, telemetry), (2.0, telemetry), (3.0, telemetry)])
        runner = StreamingTurnRunner(client, live=False, poll_interval=1.0, monotonic=clock.monotonic, sleeper=clock.sleep)
        with self.assertRaises(TurnTimeoutError) as caught:
            runner.wait_for_turn(self.make_result(), idle_timeout=3.0, hard_timeout=20.0)
        self.assertEqual(caught.exception.kind, "idle")

    def test_non_empty_delta_and_item_lifecycle_events_are_activity(self) -> None:
        activity_events = [
            event("item/agentMessage/delta", {"itemId": "a", "delta": "agent"}),
            event("item/commandExecution/outputDelta", {"itemId": "c", "delta": "output"}),
            event("item/started", {"item": {"id": "x", "type": "webSearch"}}),
            event("item/completed", {"item": {"id": "x", "type": "webSearch"}}),
        ]
        schedule = [(float(index * 2), value) for index, value in enumerate(activity_events, 1)]
        schedule.append(completed(10.0))
        _runner, client, _result, final = self.run_schedule(schedule, idle=3.0, hard=15.0)
        self.assertEqual(final.status, "completed")
        self.assertEqual(client.interrupt_count, 0)

    def test_other_explicit_activity_categories_extend_idle(self) -> None:
        schedule = [
            (2.0, event("turn/started", {"turn": {"id": "turn-1", "status": "inProgress"}})),
            (4.0, event("rawResponseItem/completed", {"item": {"type": "function_call"}})),
            (6.0, event("turn/diff/updated", {"diff": "diff --git a/a b/a"})),
            (8.0, event("warning", {"message": "retrying"})),
            (10.0, event("error", {"error": {"message": "retry"}, "willRetry": True})),
            completed(12.0),
        ]
        _runner, client, _result, final = self.run_schedule(schedule, idle=3.0, hard=15.0)
        self.assertEqual(final.status, "completed")
        self.assertEqual(client.interrupt_count, 0)

    def test_reasoning_delta_is_activity_without_raw_reasoning_retention(self) -> None:
        secret = "private-reasoning-fragment"
        schedule = [
            (2.0, event("item/reasoning/textDelta", {"delta": secret})),
            completed(4.0),
        ]
        _runner, _client, _result, final = self.run_schedule(schedule, idle=3.0, hard=10.0)
        self.assertEqual(final.reasoning_event_count, 1)
        self.assertEqual(final.reasoning_delta_chars, len(secret))
        self.assertNotIn(secret, repr(final.to_dict()))

    def test_active_silent_command_suppresses_idle_but_not_hard(self) -> None:
        start = event("item/started", {"item": {"id": "cmd", "type": "commandExecution", "command": "build"}})
        finish = event("item/completed", {"item": {"id": "cmd", "type": "commandExecution", "command": "build", "status": "completed", "exitCode": 0}})
        schedule = [(1.0, start), (7.0, finish), completed(8.0)]
        _runner, client, _result, final = self.run_schedule(schedule, idle=3.0, hard=10.0)
        self.assertEqual(final.status, "completed")
        self.assertEqual(client.interrupt_count, 0)

        clock = FakeClock()
        client = ScheduledClient(clock, [(1.0, start)])
        runner = StreamingTurnRunner(client, live=False, poll_interval=1.0, monotonic=clock.monotonic, sleeper=clock.sleep)
        with self.assertRaises(TurnTimeoutError) as caught:
            runner.wait_for_turn(self.make_result(), idle_timeout=3.0, hard_timeout=8.0)
        self.assertEqual(caught.exception.kind, "hard")

    def test_completion_available_in_final_boundary_drain_wins(self) -> None:
        clock = FakeClock()
        terminal = completed(5.0)[1]
        client = BoundaryCompletionClient(clock, terminal)
        runner = StreamingTurnRunner(client, live=False, poll_interval=1.0, monotonic=clock.monotonic, sleeper=clock.sleep)
        final = runner.wait_for_turn(self.make_result(), idle_timeout=5.0, hard_timeout=20.0)
        self.assertEqual(final.status, "completed")
        self.assertEqual(client.interrupt_count, 0)

    def test_human_approval_wait_is_not_charged(self) -> None:
        clock = FakeClock()
        request = {
            "id": 1,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        }
        client = ScheduledClient(clock, [completed(104.0)], [(1.0, request)])
        runner = StreamingTurnRunner(client, live=False, poll_interval=1.0, monotonic=clock.monotonic, sleeper=clock.sleep)

        def simulated_human_wait(result, _request):
            clock.sleep(100.0)
            result.human_approval_wait_seconds += 100.0

        runner._handle_server_request = simulated_human_wait
        final = runner.wait_for_turn(self.make_result(), idle_timeout=5.0, hard_timeout=10.0)
        self.assertEqual(final.status, "completed")

    def test_partial_command_ledger_survives_timeout(self) -> None:
        schedule = [
            (1.0, event("item/started", {"item": {"id": "cmd", "type": "commandExecution", "command": "audit"}})),
            (2.0, event("item/commandExecution/outputDelta", {"itemId": "cmd", "delta": "evidence"})),
            (3.0, event("item/completed", {"item": {"id": "cmd", "type": "commandExecution", "command": "audit", "status": "completed", "exitCode": 0}})),
        ]
        clock = FakeClock()
        client = ScheduledClient(clock, schedule)
        result = self.make_result()
        runner = StreamingTurnRunner(client, live=False, poll_interval=1.0, monotonic=clock.monotonic, sleeper=clock.sleep)
        with self.assertRaises(TurnTimeoutError) as caught:
            runner.wait_for_turn(result, idle_timeout=3.0, hard_timeout=20.0)
        self.assertIs(caught.exception.result, result)
        self.assertEqual(result.command_executions[0]["id"], "cmd")
        self.assertIn("evidence", result.command_executions[0]["classification_text"])
        self.assertEqual(client.interrupt_count, 1)

    def test_timeout_reconciliation_captures_terminal_interrupt_once(self) -> None:
        clock = FakeClock()
        client = InterruptCompletingClient(clock)
        result = self.make_result()
        runner = StreamingTurnRunner(
            client,
            live=False,
            poll_interval=1.0,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )
        with self.assertRaises(TurnTimeoutError) as caught:
            runner.wait_for_turn(result, idle_timeout=3.0, hard_timeout=20.0)
        self.assertEqual(caught.exception.kind, "idle")
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(client.interrupt_count, 1)

    def test_timeout_late_final_evidence_never_becomes_success(self) -> None:
        clock = FakeClock()
        late_final = event(
            "item/completed",
            {
                "item": {
                    "id": "final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "late evidence",
                }
            },
        )
        client = ScheduledClient(clock, [(3.1, late_final)])
        result = self.make_result()
        runner = StreamingTurnRunner(
            client,
            live=False,
            poll_interval=1.0,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )
        with self.assertRaises(TurnTimeoutError) as caught:
            runner.wait_for_turn(result, idle_timeout=3.0, hard_timeout=20.0)
        self.assertEqual(caught.exception.kind, "idle")
        self.assertEqual(result.outcome, "IDLE_TIMEOUT")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.agent_text, "late evidence")
        self.assertTrue(result.authoritative_final_evidence)


if __name__ == "__main__":
    unittest.main()
