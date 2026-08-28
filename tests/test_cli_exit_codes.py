from __future__ import annotations

import io
import sqlite3
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _bootstrap

import cx2_cli
from cx2_runtime import CX2ExecutionResult
from turn_runner import TurnRunResult


class FakeRuntime:
    def __init__(self, result):
        self.result = result

    def execute_prompt(self, **kwargs):
        return self.result

    def close(self):
        return None


def execution(status: str, *, blocked: bool = False) -> CX2ExecutionResult:
    raw = TurnRunResult("thread", "turn", status=status)
    return CX2ExecutionResult(
        blocked=blocked,
        thread_id="thread",
        session_mode="NEW",
        plan={"blocked": blocked},
        quota={},
        final_result=None,
        raw_turn_result=raw,
        attempts_used=1,
        escalations=0,
    )


class TestCLIExitCodes(unittest.TestCase):
    def test_release_version_surface(self) -> None:
        self.assertEqual(cx2_cli.CLI_VERSION, "2.0.14")
        self.assertEqual(cx2_cli.RUNTIME_VERSION, "2.0.14")
        self.assertEqual(cx2_cli.EXPECTED_ROUTER_VERSION, "1.2.2")
        output = io.StringIO()
        with patch("sys.stdout", output):
            exit_code = cx2_cli.main(["--version"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.getvalue().splitlines(),
            ["CX2 CLI 2.0.14", "CX2 runtime 2.0.14", "Router 1.2.2"],
        )

    def test_returned_turn_status_cannot_fall_through_to_success(self) -> None:
        cases = [
            (execution("completed"), 0),
            (execution("blocked", blocked=True), 2),
            (execution("failed"), 1),
            (execution("interrupted"), 130),
        ]
        for result, expected in cases:
            with self.subTest(outcome=result.outcome):
                db = sqlite3.connect(":memory:")
                try:
                    with patch("cx2_cli.CX2Runtime", return_value=FakeRuntime(result)):
                        actual = cx2_cli.execute_one_shot(
                            "prompt",
                            cwd=ROOT,
                            repo={"root": str(ROOT), "git": True},
                            db=db,
                        )
                finally:
                    db.close()
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
