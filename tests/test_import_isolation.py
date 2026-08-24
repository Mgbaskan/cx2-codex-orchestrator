import importlib
from pathlib import Path
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap


class TestImportIsolation(unittest.TestCase):
    """Regression test ensuring repository test suite imports exclusively from
    the local development workspace and never leaks into or loads from ~/.cx.
    """

    def test_key_modules_resolve_from_repo_root(self):
        cx_home = (Path.home() / ".cx").resolve()
        modules_to_verify = [
            "cx",
            "history_cli",
            "history_manager",
            "router_adapter",
            "budget_adapter",
            "cx2_cli",
            "cx2_runtime",
            "client",
            "session_adapter",
            "telemetry_adapter",
            "terminal_ui",
            "turn_runner",
            "verification_gate",
            "selection_context",
            "input_adapter",
            "cx_home",
        ]

        for mod_name in modules_to_verify:
            with self.subTest(module=mod_name):
                mod = importlib.import_module(mod_name)
                self.assertTrue(
                    hasattr(mod, "__file__") and mod.__file__ is not None,
                    f"Module {mod_name} does not have __file__ defined",
                )
                mod_path = Path(mod.__file__).resolve()
                self.assertTrue(
                    mod_path.is_relative_to(REPO_ROOT.resolve()),
                    f"Module {mod_name} resolved to {mod_path}, which is not under {REPO_ROOT}",
                )
                self.assertFalse(
                    mod_path.is_relative_to(cx_home),
                    f"Module {mod_name} resolved to {mod_path}, which leaks into production {cx_home}",
                )

    def test_sys_modules_has_no_production_cx_paths(self):
        cx_home = (Path.home() / ".cx").resolve()
        for name, mod in list(sys.modules.items()):
            if mod is not None and hasattr(mod, "__file__") and mod.__file__:
                mod_path = Path(mod.__file__).resolve()
                self.assertFalse(
                    mod_path.is_relative_to(cx_home),
                    f"sys.modules['{name}'] loaded from production runtime: {mod_path}",
                )


if __name__ == "__main__":
    unittest.main()
