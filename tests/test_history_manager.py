import unittest
import sys
import io
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

import history_cli
from history_cli import ACTIVE_SELECTION_CONTEXT


class TestHistoryManager(unittest.TestCase):

    def setUp(self):
        ACTIVE_SELECTION_CONTEXT.clear()

    def test_print_threads_formatting(self):
        data = [
            {"id": "019de6f2-0001-7000-8000-000000000001", "name": "Task One", "preview": "Task One", "cwd": r"C:\Users\example-user\Projects\sample-app", "status": "completed", "updatedAt": 1700000000.0, "createdAt": 1700000000.0, "source": "cli", "modelProvider": "gpt-5.6-luna"}
        ]
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            history_cli._print_threads({"data": data}, title="=== CX HISTORY ===")
        out = buf.getvalue()
        self.assertIn("[1]", out)
        self.assertIn("Task One", out)
        self.assertEqual(ACTIVE_SELECTION_CONTEXT.resolve("1"), "019de6f2-0001-7000-8000-000000000001")


if __name__ == "__main__":
    unittest.main()
