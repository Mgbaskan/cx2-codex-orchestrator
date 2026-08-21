from __future__ import annotations

import math
from pathlib import Path
import queue
import sys
import time
from typing import Any
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

from cx2_runtime import (
    resolve_turn_timeout,
    DEFAULT_TURN_TIMEOUTS,
    MIN_TURN_TIMEOUT_SEC,
    MAX_TURN_TIMEOUT_SEC,
)
from turn_runner import StreamingTurnRunner, TurnRunResult


class FakeClient:
    def __init__(self):
        self.server_requests_q = queue.Queue()
        self.notifications_q = queue.Queue()
        self.unknown_q = queue.Queue()
        self.interrupt_calls = []
        self.interrupt_should_raise = False

    def drain_server_requests(self):
        items = []
        while not self.server_requests_q.empty():
            items.append(self.server_requests_q.get_nowait())
        return items

    def drain_notifications(self):
        items = []
        while not self.notifications_q.empty():
            items.append(self.notifications_q.get_nowait())
        return items

    def drain_unknown(self):
        items = []
        while not self.unknown_q.empty():
            items.append(self.unknown_q.get_nowait())
        return items

    def request(self, method: str, params: dict[str, Any], timeout: float = 15.0):
        if method == "turn/interrupt":
            self.interrupt_calls.append((params, timeout))
            if self.interrupt_should_raise:
                raise RuntimeError("App Server transport error during interrupt")
            return {"status": "ok"}
        return {}


class TestTurnTimeout(unittest.TestCase):

    # =========================================================================
    # 1. Timeout Policy Resolver Tests (Route & Policy Aware)
    # =========================================================================

    def test_default_tier_timeouts(self):
        """B, C, D: Defaults must be routine=300, standard=450, deep=600."""
        self.assertEqual(resolve_turn_timeout("routine"), 300.0)
        self.assertEqual(resolve_turn_timeout("standard"), 450.0)
        self.assertEqual(resolve_turn_timeout("deep"), 600.0)
        self.assertEqual(resolve_turn_timeout({"tier": "deep"}), 600.0)
        self.assertEqual(resolve_turn_timeout({"tier": "standard"}), 450.0)
        self.assertEqual(resolve_turn_timeout({"tier": "routine"}), 300.0)

    def test_escalation_attempt_tier(self):
        """E: Standard task escalated to deep attempt must resolve to deep timeout (600s)."""
        base_route = {"tier": "standard", "reasoning": "medium"}
        attempt_route = dict(base_route, tier="deep", reasoning="high")
        self.assertEqual(resolve_turn_timeout(attempt_route), 600.0)

    def test_old_policy_backward_compatibility(self):
        """F: Old policy without execution section resolves to safe defaults."""
        old_policy = {"tiers": {"routine": "gpt-5.6-terra"}}
        self.assertEqual(resolve_turn_timeout("deep", policy=old_policy), 600.0)
        self.assertEqual(resolve_turn_timeout("standard", policy=old_policy), 450.0)
        self.assertEqual(resolve_turn_timeout("routine", policy=old_policy), 300.0)

    def test_custom_policy_override(self):
        """G: Custom policy override takes effect."""
        custom_policy = {
            "execution": {
                "turn_timeout_sec": {
                    "routine": 200,
                    "standard": 500,
                    "deep": 900,
                }
            }
        }
        self.assertEqual(resolve_turn_timeout("deep", policy=custom_policy), 900.0)
        self.assertEqual(resolve_turn_timeout("standard", policy=custom_policy), 500.0)
        self.assertEqual(resolve_turn_timeout("routine", policy=custom_policy), 200.0)

    def test_invalid_policy_values(self):
        """H: Non-numeric, boolean, NaN, Inf, and out-of-bounds values handled safely."""
        # Non-numeric string => default
        p_str = {"execution": {"turn_timeout_sec": {"deep": "invalid"}}}
        self.assertEqual(resolve_turn_timeout("deep", policy=p_str), 600.0)

        # Boolean True/False => default (rejected)
        p_bool = {"execution": {"turn_timeout_sec": {"deep": True}}}
        self.assertEqual(resolve_turn_timeout("deep", policy=p_bool), 600.0)

        # NaN / Inf => default
        p_nan = {"execution": {"turn_timeout_sec": {"deep": float("nan")}}}
        self.assertEqual(resolve_turn_timeout("deep", policy=p_nan), 600.0)
        p_inf = {"execution": {"turn_timeout_sec": {"deep": float("inf")}}}
        self.assertEqual(resolve_turn_timeout("deep", policy=p_inf), 600.0)

        # Negative / zero => clamped to MIN_TURN_TIMEOUT_SEC (30.0)
        p_neg = {"execution": {"turn_timeout_sec": {"deep": -10}}}
        self.assertEqual(resolve_turn_timeout("deep", policy=p_neg), 30.0)
        p_zero = {"execution": {"turn_timeout_sec": {"deep": 0}}}
        self.assertEqual(resolve_turn_timeout("deep", policy=p_zero), 30.0)

        # Excessively large => clamped to MAX_TURN_TIMEOUT_SEC (1800.0)
        p_huge = {"execution": {"turn_timeout_sec": {"deep": 999999}}}
        self.assertEqual(resolve_turn_timeout("deep", policy=p_huge), 1800.0)

    # =========================================================================
    # 2. Timeout Lifecycle Safety Tests (Synthetic AppServer Client)
    # =========================================================================

    def test_turn_timeout_calls_interrupt_once(self):
        """1 & 2: When deadline expires, turn/interrupt must be called once and TimeoutError raised."""
        client = FakeClient()
        runner = StreamingTurnRunner(client, live=False)
        runner.poll_interval = 0.001

        result = TurnRunResult(thread_id="th_123", turn_id="tu_456")
        
        # Test with very short synthetic timeout (0.01s)
        with self.assertRaises(TimeoutError):
            runner.wait_for_turn(result, timeout=0.01)

        self.assertEqual(len(client.interrupt_calls), 1)
        params, to = client.interrupt_calls[0]
        self.assertEqual(params.get("threadId"), "th_123")
        self.assertEqual(params.get("turnId"), "tu_456")
        self.assertEqual(result.status, "failed")

    def test_turn_timeout_interrupt_failure_preserves_timeout_error(self):
        """3: If interrupt request fails, original TimeoutError must still be raised."""
        client = FakeClient()
        client.interrupt_should_raise = True
        runner = StreamingTurnRunner(client, live=False)
        runner.poll_interval = 0.001

        result = TurnRunResult(thread_id="th_123", turn_id="tu_456")

        with self.assertRaises(TimeoutError):
            runner.wait_for_turn(result, timeout=0.01)

        self.assertEqual(len(client.interrupt_calls), 1)

    def test_normal_completed_turn_does_not_call_interrupt(self):
        """6: Normal completed turn does not trigger interrupt."""
        client = FakeClient()
        runner = StreamingTurnRunner(client, live=False)
        runner.poll_interval = 0.001

        result = TurnRunResult(thread_id="th_123", turn_id="tu_456")
        # Simulate turn/completed notification
        client.notifications_q.put({
            "method": "turn/completed",
            "params": {"threadId": "th_123", "turn": {"id": "tu_456", "status": "completed"}},
        })

        res = runner.wait_for_turn(result, timeout=1.0)
        self.assertEqual(res.status, "completed")
        self.assertEqual(len(client.interrupt_calls), 0)


if __name__ == "__main__":
    unittest.main()
