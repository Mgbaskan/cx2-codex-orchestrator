import unittest
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "runtime" / "cx2"))

from verification_gate import (
    classify_file,
    determine_dominant_category,
    classify_command,
    unwrap_display_command,
    is_command_masked,
)


class TestVerificationGate(unittest.TestCase):

    def test_classify_source_code_files(self):
        self.assertEqual(classify_file("src/index.ts"), "SOURCE_CODE")
        self.assertEqual(classify_file("lib/util.py"), "SOURCE_CODE")
        self.assertEqual(classify_file("main.go"), "SOURCE_CODE")

    def test_classify_docs_only_files(self):
        self.assertEqual(classify_file("README.md"), "DOCS_ONLY")
        self.assertEqual(classify_file("docs/architecture.md"), "DOCS_ONLY")

    def test_dominant_category_resolution(self):
        categories = {"DOCS_ONLY", "SOURCE_CODE", "CONFIG_BUILD"}
        dominant = determine_dominant_category(categories)
        self.assertEqual(dominant, "SOURCE_CODE")

    def test_classify_commands(self):
        self.assertIn("TEST", classify_command("npm test"))
        self.assertIn("TEST", classify_command("pytest tests/"))
        self.assertIn("BUILD", classify_command("npm run build"))

    def test_masked_command_detection(self):
        self.assertTrue(is_command_masked("npm test || true"))
        self.assertTrue(is_command_masked("npm test ; exit 0"))
        self.assertFalse(is_command_masked("npm test"))


if __name__ == "__main__":
    unittest.main()
