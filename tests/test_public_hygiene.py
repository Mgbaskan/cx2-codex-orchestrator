from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
import _bootstrap

from scripts.check_public_hygiene import scan_text


class TestPublicHygiene(unittest.TestCase):
    def categories(self, text: str) -> set[str]:
        return {item.category for item in scan_text("fixture.txt", text)}

    def test_known_private_identifier_is_rejected(self) -> None:
        private_name = "hi" + "brit"
        self.assertIn(
            "private project identifier",
            self.categories(f"audit {private_name} repository"),
        )

    def test_real_windows_user_path_is_rejected(self) -> None:
        separator = "\\"
        real_path = separator.join(
            ("C:", "Users", "actual-developer", "Projects", "service")
        )
        self.assertIn(
            "developer user path",
            self.categories(real_path),
        )

    def test_synthetic_windows_user_path_is_allowed(self) -> None:
        self.assertEqual(
            self.categories(r"C:\Users\example-user\Projects\sample-app"),
            set(),
        )

    def test_escaped_drive_path_is_not_mistaken_for_unc(self) -> None:
        self.assertEqual(
            self.categories(r'path = "C:\\Custom\\Target"'),
            set(),
        )

    def test_real_unc_path_is_rejected(self) -> None:
        separator = "\\"
        real_unc = separator * 2 + separator.join(
            ("private-host", "share", "artifact.txt")
        )
        self.assertIn(
            "private UNC path",
            self.categories(real_unc),
        )

    def test_high_confidence_secret_is_redacted_by_category(self) -> None:
        candidate = "ghp_" + "a" * 24
        self.assertIn("credential candidate", self.categories(candidate))

    def test_clean_public_fixture_is_allowed(self) -> None:
        self.assertEqual(
            self.categories("sample-project uses a disposable test workspace"),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
