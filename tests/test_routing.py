import unittest
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

import cx


class TestRouting(unittest.TestCase):

    def setUp(self):
        self.policy = {
            "version": 1,
            "models": {
                "routine": ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"],
                "standard": ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"],
                "deep": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
            },
            "reasoning": {
                "routine": "low",
                "standard": "medium",
                "deep": "high"
            },
            "thresholds": {
                "routine_max": 1,
                "deep_min": 7
            }
        }
        self.repo = {"git": True, "clean": True}

    def test_routine_read_only_classification(self):
        res = cx.classify("what is the current git status?", self.repo, self.policy)
        self.assertEqual(res["tier"], "routine")
        self.assertEqual(res["sandbox"], "read-only")
        self.assertEqual(res["reasoning"], "low")

    def test_mutation_workspace_write_classification(self):
        res = cx.classify("refactor auth module and update unit tests", self.repo, self.policy)
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertIn(res["tier"], {"standard", "deep"})

    def test_write_negation_awareness(self):
        stripped = cx.strip_negated_write_phrases("do not modify any files, just explain how auth works")
        self.assertNotIn("modify any files", stripped)
        res = cx.classify("do not modify any files, just explain how auth works", self.repo, self.policy)
        self.assertEqual(res["sandbox"], "read-only")


if __name__ == "__main__":
    unittest.main()
