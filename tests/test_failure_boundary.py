import unittest
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

import cx


class TestFailureBoundary(unittest.TestCase):

    def test_infrastructure_failure_detection(self):
        self.assertTrue(cx.is_infrastructure_failure("permission denied while opening port"))
        self.assertTrue(cx.is_infrastructure_failure("rate limit exceeded for model"))
        self.assertTrue(cx.is_infrastructure_failure("docker daemon is not running"))
        self.assertFalse(cx.is_infrastructure_failure("SyntaxError: unexpected token"))


if __name__ == "__main__":
    unittest.main()
