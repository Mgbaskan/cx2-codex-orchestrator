import unittest
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "runtime" / "cx2"))

from selection_context import SelectionContext
from history_manager import HistoryManagerError


class TestNumericSelection(unittest.TestCase):

    def setUp(self):
        self.ctx = SelectionContext()

    def test_set_and_resolve_valid_indices(self):
        entries = [
            {"id": "019de6f2-0000-7000-8000-000000000001", "nameOrPreview": "First Task"},
            {"id": "019de6f2-0000-7000-8000-000000000002", "nameOrPreview": "Second Task"},
        ]
        self.ctx.set_entries("history", "History", entries)
        self.assertEqual(self.ctx.resolve("1"), "019de6f2-0000-7000-8000-000000000001")
        self.assertEqual(self.ctx.resolve("2"), "019de6f2-0000-7000-8000-000000000002")

    def test_native_uuid_unaffected_by_context(self):
        uuid_str = "019de6f2-aaaa-7000-8000-000000000099"
        self.assertEqual(self.ctx.resolve(uuid_str), uuid_str)

    def test_out_of_range_numeric_raises_error(self):
        self.ctx.set_entries("history", "History", [{"id": "uuid-1"}])
        with self.assertRaises(HistoryManagerError):
            self.ctx.resolve("99")

    def test_invalid_numeric_formats_rejected(self):
        self.ctx.set_entries("history", "History", [{"id": "uuid-1"}])
        for invalid in ["0", "01", "-1", "+1"]:
            with self.assertRaises(HistoryManagerError):
                self.ctx.resolve(invalid)

    def test_clear_invalidates_context(self):
        self.ctx.set_entries("history", "History", [{"id": "uuid-1"}])
        self.ctx.clear()
        with self.assertRaises(HistoryManagerError):
            self.ctx.resolve("1")


if __name__ == "__main__":
    unittest.main()
