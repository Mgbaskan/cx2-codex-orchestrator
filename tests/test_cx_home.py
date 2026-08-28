import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

from cx_home import _is_subpath, is_installed_runtime, resolve_cx_home
import cx_home
import client
import cx2_cli
import cx2_runtime
import turn_runner
import session_adapter
import router_adapter
import prompt_transport
import history_manager
import history_cli
import budget_adapter
import telemetry_adapter
import cx as production_cx
from scripts import run_isolated_tests


class TestCxHomeResolver(unittest.TestCase):
    """Deterministic regression suite for authoritative CX_HOME resolution."""

    def test_custom_installed_root_windows_layout(self):
        """Installed runtime on Windows resolves strictly to <root> independent of USERPROFILE/HOME."""
        target_root = Path(r"C:\Custom\Target\.cx")
        mock_module = target_root / "runtime" / "cx2" / "cx_home.py"
        mock_exe = target_root / "runtime" / "venv" / "Scripts" / "python.exe"

        self.assertTrue(
            is_installed_runtime(module_file=str(mock_module), executable=str(mock_exe))
        )
        resolved = resolve_cx_home(module_file=str(mock_module), executable=str(mock_exe))
        self.assertEqual(resolved, target_root.resolve())

    def test_custom_installed_root_posix_layout(self):
        """Installed runtime with POSIX layout resolves strictly to <root>."""
        target_root = Path("/opt/custom/target/.cx")
        mock_module = target_root / "runtime" / "cx2" / "cx_home.py"
        mock_exe = target_root / "runtime" / "venv" / "bin" / "python"

        self.assertTrue(
            is_installed_runtime(module_file=str(mock_module), executable=str(mock_exe))
        )
        resolved = resolve_cx_home(module_file=str(mock_module), executable=str(mock_exe))
        self.assertEqual(resolved, target_root.resolve())

    def test_source_development_mode_not_detected_as_installed(self):
        """Repository source run with .venv must NOT identify repo root as installed runtime."""
        repo_root = Path(r"C:\Users\example-user\Projects\sample-project")
        mock_module = repo_root / "runtime" / "cx2" / "cx_home.py"
        mock_exe = repo_root / ".venv" / "Scripts" / "python.exe"

        self.assertFalse(
            is_installed_runtime(module_file=str(mock_module), executable=str(mock_exe))
        )
        resolved = resolve_cx_home(module_file=str(mock_module), executable=str(mock_exe))
        expected_fallback = (Path.home() / ".cx").resolve()
        self.assertEqual(resolved, expected_fallback)

    def test_sibling_prefix_containment_negative_cases(self):
        """Boundary-safe containment must reject sibling prefixes like venv-other and cx2-old."""
        target_root = Path(r"C:\Custom\Target\.cx")
        valid_module = target_root / "runtime" / "cx2" / "cx_home.py"
        valid_exe = target_root / "runtime" / "venv" / "Scripts" / "python.exe"

        # 1. venv-other sibling
        sibling_exe = target_root / "runtime" / "venv-other" / "Scripts" / "python.exe"
        self.assertFalse(
            _is_subpath(sibling_exe, target_root / "runtime" / "venv")
        )
        self.assertFalse(
            is_installed_runtime(module_file=str(valid_module), executable=str(sibling_exe))
        )

        # 2. venv_backup sibling
        backup_exe = target_root / "runtime" / "venv_backup" / "python.exe"
        self.assertFalse(
            _is_subpath(backup_exe, target_root / "runtime" / "venv")
        )
        self.assertFalse(
            is_installed_runtime(module_file=str(valid_module), executable=str(backup_exe))
        )

        # 3. cx2-old sibling module
        sibling_module = target_root / "runtime" / "cx2-old" / "cx_home.py"
        self.assertFalse(
            is_installed_runtime(module_file=str(sibling_module), executable=str(valid_exe))
        )

        # 4. cx2_backup sibling module
        backup_module = target_root / "runtime" / "cx2_backup" / "cx_home.py"
        self.assertFalse(
            is_installed_runtime(module_file=str(backup_module), executable=str(valid_exe))
        )

    def test_windows_case_insensitivity(self):
        """Windows paths with mismatched casing must resolve identically."""
        target_root = Path(r"C:\CustomApp\Target\.cx")
        mock_module = Path(r"c:\customapp\target\.cx\runtime\cx2\cx_home.py")
        mock_exe = Path(r"C:\CUSTOMAPP\TARGET\.CX\RUNTIME\VENV\SCRIPTS\PYTHON.EXE")

        self.assertTrue(
            is_installed_runtime(module_file=str(mock_module), executable=str(mock_exe))
        )
        resolved = resolve_cx_home(module_file=str(mock_module), executable=str(mock_exe))
        self.assertEqual(
            os.path.normcase(str(resolved)),
            os.path.normcase(str(target_root.resolve())),
        )

    def test_paths_with_spaces(self):
        """Installation paths containing spaces must resolve correctly."""
        target_root = Path(r"C:\Program Files\Custom CX Location\.cx")
        mock_module = target_root / "runtime" / "cx2" / "cx_home.py"
        mock_exe = target_root / "runtime" / "venv" / "Scripts" / "python.exe"

        self.assertTrue(
            is_installed_runtime(module_file=str(mock_module), executable=str(mock_exe))
        )
        resolved = resolve_cx_home(module_file=str(mock_module), executable=str(mock_exe))
        self.assertEqual(resolved, target_root.resolve())

    def test_paths_with_non_ascii_characters(self):
        """Installation paths containing non-ASCII characters must resolve correctly."""
        target_root = Path(r"C:\Kullanıcılar\Örnek Geliştirme\.cx")
        mock_module = target_root / "runtime" / "cx2" / "cx_home.py"
        mock_exe = target_root / "runtime" / "venv" / "Scripts" / "python.exe"

        self.assertTrue(
            is_installed_runtime(module_file=str(mock_module), executable=str(mock_exe))
        )
        resolved = resolve_cx_home(module_file=str(mock_module), executable=str(mock_exe))
        self.assertEqual(resolved, target_root.resolve())

    def test_side_by_side_installations_are_isolated(self):
        """Two installations side-by-side must resolve independently with zero cross-leakage."""
        root_a = Path(r"C:\Temp\Install_A\.cx")
        mod_a = root_a / "runtime" / "cx2" / "cx_home.py"
        exe_a = root_a / "runtime" / "venv" / "Scripts" / "python.exe"

        root_b = Path(r"C:\Temp\Install_B\.cx")
        mod_b = root_b / "runtime" / "cx2" / "cx_home.py"
        exe_b = root_b / "runtime" / "venv" / "Scripts" / "python.exe"

        res_a = resolve_cx_home(module_file=str(mod_a), executable=str(exe_a))
        res_b = resolve_cx_home(module_file=str(mod_b), executable=str(exe_b))

        self.assertEqual(res_a, root_a.resolve())
        self.assertEqual(res_b, root_b.resolve())
        self.assertNotEqual(res_a, res_b)

    def test_all_active_modules_share_identical_cx_home(self):
        """Every active migrated module must resolve the exact same CX_HOME in the current process."""
        expected_home = cx_home.resolve_cx_home()

        self.assertEqual(production_cx.CX_HOME, expected_home)
        self.assertEqual(client.CX_HOME, expected_home)
        self.assertEqual(cx2_cli.CX_HOME, expected_home)
        self.assertEqual(cx2_runtime.CX_HOME, expected_home)
        self.assertEqual(turn_runner.CX_HOME, expected_home)
        self.assertEqual(session_adapter.CX_HOME, expected_home)
        self.assertEqual(router_adapter.CX_HOME, expected_home)
        self.assertEqual(prompt_transport.CX_HOME, expected_home)
        self.assertEqual(history_manager.CX_HOME, expected_home)
        self.assertEqual(history_cli.CX_HOME, expected_home)
        self.assertEqual(budget_adapter.CX_HOME, expected_home)
        self.assertEqual(telemetry_adapter.CX_HOME, expected_home)

    def test_repository_write_targets_stay_in_disposable_home(self):
        """Repository imports must never target the original user's CX state."""
        disposable_home = _bootstrap.TEST_USER_HOME.resolve()
        disposable_cx = (disposable_home / ".cx").resolve()
        original_cx = (_bootstrap.ORIGINAL_USER_HOME / ".cx").resolve()

        self.assertEqual(Path.home().resolve(), disposable_home)
        self.assertEqual(Path(os.environ["USERPROFILE"]).resolve(), disposable_home)
        self.assertEqual(Path(os.environ["HOME"]).resolve(), disposable_home)
        self.assertEqual(Path(tempfile.gettempdir()).resolve(), _bootstrap.TEST_TEMP_ROOT)
        for temp_name in ("TEMP", "TMP", "TMPDIR"):
            self.assertEqual(
                Path(os.environ[temp_name]).resolve(), _bootstrap.TEST_TEMP_ROOT
            )
        self.assertNotEqual(disposable_cx, original_cx)
        self.assertEqual(resolve_cx_home(), disposable_cx)

        configured_root = os.environ.get("CX2_TEST_TEMP_ROOT")
        if configured_root:
            self.assertEqual(
                _bootstrap.TEST_USER_HOME.parent,
                Path(os.path.abspath(configured_root)),
            )

        targets = {
            "crash log": cx2_cli.CX2_HOME / "cx2-cli-last-crash.txt",
            "App Server stderr": client.STDERR_FILE,
            "usage/session DB": production_cx.DB_FILE,
            "policy": production_cx.POLICY_FILE,
            "runtime log": production_cx.LOG_FILE,
            "model cache": production_cx.MODEL_CACHE_FILE,
            "quota state": production_cx.QUOTA_FILE,
        }
        for label, target in targets.items():
            with self.subTest(target=label):
                resolved = target.resolve()
                self.assertTrue(_is_subpath(resolved, disposable_cx))
                self.assertFalse(_is_subpath(resolved, original_cx))

    def test_diagnostic_write_spies_use_disposable_targets(self):
        """Exercise diagnostic write paths with spies; no filesystem write occurs."""
        expected_crash = (
            _bootstrap.TEST_USER_HOME
            / ".cx"
            / "runtime"
            / "cx2"
            / "cx2-cli-last-crash.txt"
        ).resolve()
        with patch.object(Path, "mkdir", autospec=True), patch.object(
            Path, "write_text", autospec=True
        ) as write_text:
            cx2_cli._write_crash_log(RuntimeError("synthetic isolation probe"))
        self.assertEqual(write_text.call_count, 1)
        self.assertEqual(write_text.call_args.args[0].resolve(), expected_crash)

        class _StoppedProcess:
            stdin = None
            stdout = None
            stderr = None

            @staticmethod
            def wait(timeout=None):
                return 0

        app_server = client.AppServerClient(Path("synthetic-codex.exe"))
        app_server.process = _StoppedProcess()
        with patch.object(Path, "write_text", autospec=True) as write_text:
            app_server.close()
        expected_stderr = (
            _bootstrap.TEST_USER_HOME
            / ".cx"
            / "runtime"
            / "cx2"
            / "app-server-stderr.log"
        ).resolve()
        self.assertEqual(write_text.call_count, 1)
        self.assertEqual(write_text.call_args.args[0].resolve(), expected_stderr)

    def test_isolated_runner_outer_temp_avoids_synthetic_agent_cache(self):
        outer_temp = run_isolated_tests.resolve_isolated_temp_parent()
        synthetic_agent_cache = _bootstrap.TEST_TEMP_ROOT / "synthetic-user" / ".agent-cache"
        self.assertFalse(_is_subpath(outer_temp, synthetic_agent_cache))
        configured_root = os.environ.get("CX2_TEST_TEMP_ROOT")
        if configured_root:
            self.assertEqual(outer_temp, Path(os.path.abspath(configured_root)))
        elif os.environ.get("LOCALAPPDATA"):
            self.assertEqual(
                outer_temp,
                (Path(os.environ["LOCALAPPDATA"]) / "Temp").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
