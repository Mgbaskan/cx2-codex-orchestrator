from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _bootstrap  # noqa: E402
sys.path.insert(0, str(_bootstrap.RUNTIME_DIR))

from file_write_grants import (  # noqa: E402
    FileWriteGrantRegistry,
    ordinary_workspace_file_mutation,
)
from terminal_ui import TerminalRenderer  # noqa: E402
from turn_runner import StreamingTurnRunner, TurnRunResult  # noqa: E402


class _ApprovalClient:
    def __init__(self) -> None:
        self.responses = []

    def respond(self, request_id, result) -> None:
        self.responses.append((request_id, result))

    def respond_error(self, request_id, code, message) -> None:
        raise AssertionError((request_id, code, message))


class TestFileWriteGrants(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(r"C:\workspace\project")

    def test_key_is_runtime_thread_workspace_and_kind_scoped(self) -> None:
        grants = FileWriteGrantRegistry("nonce-a")
        grants.grant(thread_id="thread-a", workspace_root=self.root)
        self.assertTrue(grants.has(thread_id="thread-a", workspace_root=self.root))
        self.assertFalse(grants.has(thread_id="thread-b", workspace_root=self.root))
        other_runtime = FileWriteGrantRegistry("nonce-b")
        self.assertFalse(other_runtime.has(thread_id="thread-a", workspace_root=self.root))

    def test_ordinary_changes_only(self) -> None:
        self.assertTrue(
            ordinary_workspace_file_mutation(
                {"fileChanges": {"src/a.py": {"action": "edit"}}},
                workspace_root=self.root,
            )
        )
        self.assertTrue(
            ordinary_workspace_file_mutation(
                {"fileChanges": {"src/shell.py": {"action": "edit"}}},
                workspace_root=self.root,
            )
        )
        self.assertFalse(
            ordinary_workspace_file_mutation(
                {"fileChanges": {"src/a.py": {"action": "delete"}}},
                workspace_root=self.root,
            )
        )
        self.assertFalse(
            ordinary_workspace_file_mutation(
                {"fileChanges": {r"C:\other\a.py": {"action": "edit"}}},
                workspace_root=self.root,
            )
        )
        self.assertTrue(
            ordinary_workspace_file_mutation(
                {"fileChanges": {"git reset --hard": {"action": "edit"}}},
                workspace_root=self.root,
            )
        )

    def test_clear_invalidates(self) -> None:
        grants = FileWriteGrantRegistry("nonce")
        grants.grant(thread_id="thread", workspace_root=self.root)
        grants.clear()
        self.assertFalse(grants.has(thread_id="thread", workspace_root=self.root))

    def test_metadata_without_target_fails_closed(self) -> None:
        self.assertFalse(
            ordinary_workspace_file_mutation(
                {"fileChanges": {"action": "edit"}}, workspace_root=self.root
            )
        )
        self.assertFalse(
            ordinary_workspace_file_mutation(
                {"changes": [{"kind": "patch", "reason": "no target"}]},
                workspace_root=self.root,
            )
        )

    def test_modern_explicit_paths_and_legacy_maps_share_policy(self) -> None:
        modern = {"fileChanges": [{"path": "src/a.py", "action": "edit"}]}
        legacy = {"fileChanges": {"src/a.py": {"action": "edit"}}}
        self.assertTrue(ordinary_workspace_file_mutation(modern, workspace_root=self.root))
        self.assertTrue(ordinary_workspace_file_mutation(legacy, workspace_root=self.root))
        for payload in (
            {"fileChanges": [{"path": "../escape.py", "action": "edit"}]},
            {"fileChanges": {"src/a.py": {"action": "edit"}}, "dangerFullAccess": True},
            {"fileChanges": {"src/a.py": {"action": "edit"}}, "shell": "git clean -fd"},
            {"fileChanges": {"src/a.py": {"action": "rename"}}},
            {"fileChanges": [{"path": "bad\x00path", "action": "edit"}]},
        ):
            self.assertFalse(ordinary_workspace_file_mutation(payload, workspace_root=self.root))

    def test_turn_runner_modern_and_legacy_share_runtime_grant(self) -> None:
        client = _ApprovalClient()
        registry = FileWriteGrantRegistry("runtime-a")
        runner = StreamingTurnRunner(client, live=True, file_write_grants=registry)
        runner.current_cwd = self.root
        runner._active_thread_id = "thread-a"
        result = TurnRunResult(thread_id="thread-a", turn_id="turn-a")
        modern = {
            "id": "modern",
            "method": "item/fileChange/requestApproval",
            "params": {"fileChanges": [{"path": "src/a.py", "action": "edit"}]},
        }
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="acceptForSession") as prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                runner._handle_server_request(result, modern)
        self.assertEqual(prompt.call_count, 1)
        self.assertTrue(registry.has(thread_id="thread-a", workspace_root=self.root))

        legacy = {
            "id": "legacy",
            "method": "applyPatchApproval",
            "params": {"fileChanges": {"src/b.py": {"action": "edit"}}},
        }
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="denied") as prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                runner._handle_server_request(result, legacy)
        self.assertEqual(prompt.call_count, 0)
        self.assertEqual(client.responses[-1], ("legacy", {"decision": "approved_for_session"}))

        runner._active_thread_id = "thread-b"
        with patch("turn_runner._CX2_TERMINAL.approval_prompt", return_value="denied") as prompt:
            with patch.object(TerminalRenderer, "can_prompt", new=property(lambda self: True)):
                runner._handle_server_request(
                    TurnRunResult(thread_id="thread-b", turn_id="turn-b"),
                    {**legacy, "id": "other-thread"},
                )
        self.assertEqual(prompt.call_count, 1)


if __name__ == "__main__":
    unittest.main()
