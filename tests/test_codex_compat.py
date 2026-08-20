import unittest
from unittest.mock import patch, MagicMock
import subprocess
import tempfile
import sqlite3
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

from codex_compat import (
    VALIDATED_CODEX_VERSION,
    VALIDATED_CODEX_PACKAGE,
    VALIDATED_CLI_BIN_PACKAGE,
    REASON_PRE42_COMPATIBLE,
    REASON_POST42_INCOMPATIBLE,
    REASON_VERSION_UNAVAILABLE,
    REASON_UNVALIDATED_VERSION,
    REASON_STATE_DB_MISSING,
    REASON_STATE_SCHEMA_UNVALIDATED,
    REASON_PACKAGE_VERSION_MISMATCH,
    CompatibilityState,
    SemVer,
    parse_codex_version,
    detect_codex_binary_version,
    clear_version_cache,
    detect_installed_package_versions,
    check_requirements_consistency,
    evaluate_native_delete_safety,
    assess_codex_compatibility,
    generate_doctor_compatibility_summary,
    CORE_REQUIRED_METHODS,
    THREAD_MANAGEMENT_METHODS,
    INTERACTIVE_REQUEST_METHODS,
    STREAMED_NOTIFICATION_METHODS,
)
import history_manager
from client import AppServerClient


class TestCodexCompat(unittest.TestCase):

    def setUp(self):
        clear_version_cache()

    def tearDown(self):
        clear_version_cache()

    # =========================================================================
    # VERSION PARSING TESTS
    # =========================================================================

    def test_parse_exact_version(self):
        """A. Parse exact version '0.144.4'."""
        ver = parse_codex_version("0.144.4")
        self.assertIsNotNone(ver)
        self.assertEqual(ver.major, 0)
        self.assertEqual(ver.minor, 144)
        self.assertEqual(ver.patch, 4)
        self.assertIsNone(ver.prerelease)
        self.assertEqual(str(ver), "0.144.4")

    def test_parse_codex_cli_prefix(self):
        """B. Parse 'codex-cli 0.144.4'."""
        ver = parse_codex_version("codex-cli 0.144.4")
        self.assertIsNotNone(ver)
        self.assertEqual(ver.tuple(), (0, 144, 4))
        self.assertIsNone(ver.prerelease)

    def test_parse_prerelease(self):
        """C. Parse '0.148.0-alpha.9' and 'codex-cli 0.148.0-alpha.9'."""
        ver1 = parse_codex_version("0.148.0-alpha.9")
        self.assertIsNotNone(ver1)
        self.assertEqual(ver1.tuple(), (0, 148, 0))
        self.assertEqual(ver1.prerelease, "alpha.9")
        self.assertEqual(str(ver1), "0.148.0-alpha.9")

        ver2 = parse_codex_version("codex-cli 0.148.0-alpha.9")
        self.assertIsNotNone(ver2)
        self.assertEqual(ver2.tuple(), (0, 148, 0))
        self.assertEqual(ver2.prerelease, "alpha.9")

    def test_parse_malformed_returns_none(self):
        """D. Malformed or invalid input gracefully returns None without crashing."""
        self.assertIsNone(parse_codex_version(None))
        self.assertIsNone(parse_codex_version(""))
        self.assertIsNone(parse_codex_version("   "))
        self.assertIsNone(parse_codex_version("not a version"))
        self.assertIsNone(parse_codex_version("abc.def.ghi"))

    def test_semver_comparisons(self):
        """SemVer comparison logic."""
        v144 = parse_codex_version("0.144.4")
        v148 = parse_codex_version("0.148.0")
        v148_alpha = parse_codex_version("0.148.0-alpha.9")

        self.assertTrue(v144 < v148)
        self.assertTrue(v148_alpha < v148)
        self.assertTrue(v144 < v148_alpha)
        self.assertEqual(v144, parse_codex_version("0.144.4"))
        self.assertNotEqual(v144, v148)

    # =========================================================================
    # COMPATIBILITY EVALUATION TESTS
    # =========================================================================

    def test_exact_validated_version_is_supported(self):
        """E. Exact validated version 0.144.4 produces SUPPORTED core state."""
        with patch("codex_compat.detect_codex_binary_version", return_value=("codex-cli 0.144.4", None)):
            with patch("codex_compat.evaluate_native_delete_safety", return_value={"supported": True, "reason": REASON_PRE42_COMPATIBLE}):
                with patch("codex_compat.detect_installed_package_versions", return_value={"openai-codex": "0.144.4", "openai-codex-cli-bin": "0.144.4"}):
                    report = assess_codex_compatibility(use_cache=False)
                    self.assertEqual(report.core_state, CompatibilityState.SUPPORTED)
                    self.assertEqual(report.overall_state, CompatibilityState.SUPPORTED)
                    self.assertFalse(report.is_fatal)
                    self.assertFalse(report.package_mismatch)
                    self.assertEqual(len(report.issues), 0)

    def test_unknown_newer_version_is_unverified(self):
        """F. Newer version 0.148.0-alpha.9 produces UNVERIFIED core state, not automatically SUPPORTED."""
        with patch("codex_compat.detect_codex_binary_version", return_value=("codex-cli 0.148.0-alpha.9", None)):
            with patch("codex_compat.evaluate_native_delete_safety", return_value={"supported": False, "reason": REASON_UNVALIDATED_VERSION}):
                with patch("codex_compat.detect_installed_package_versions", return_value={"openai-codex": "0.148.0-alpha.9", "openai-codex-cli-bin": "0.148.0-alpha.9"}):
                    report = assess_codex_compatibility(use_cache=False)
                    self.assertEqual(report.core_state, CompatibilityState.UNVERIFIED)
                    self.assertEqual(report.overall_state, CompatibilityState.UNVERIFIED)
                    self.assertFalse(report.is_fatal)
                    self.assertTrue(any("differ" in w or "newer" in w for w in report.warnings))

    def test_package_version_mismatch_never_supported(self):
        """Invariant: Package version mismatch must NEVER return SUPPORTED."""
        with patch("codex_compat.detect_codex_binary_version", return_value=("codex-cli 0.144.4", None)):
            with patch("codex_compat.evaluate_native_delete_safety", return_value={"supported": True, "reason": REASON_PRE42_COMPATIBLE}):
                with patch("codex_compat.detect_installed_package_versions", return_value={"openai-codex": "0.144.4", "openai-codex-cli-bin": "0.148.0"}):
                    report = assess_codex_compatibility(use_cache=False)
                    self.assertNotEqual(report.overall_state, CompatibilityState.SUPPORTED)
                    self.assertNotEqual(report.overall_state, CompatibilityState.SUPPORTED_WITH_DEGRADATION)
                    self.assertEqual(report.overall_state, CompatibilityState.UNVERIFIED)
                    self.assertTrue(report.package_mismatch)
                    self.assertEqual(report.package_mismatch_reason, REASON_PACKAGE_VERSION_MISMATCH)
                    self.assertFalse(report.is_fatal)
                    self.assertTrue(any(REASON_PACKAGE_VERSION_MISMATCH in w for w in report.warnings))

    def test_same_version_newer_pair_is_unverified(self):
        """Same non-baseline pair (0.148.0 / 0.148.0) is UNVERIFIED."""
        with patch("codex_compat.detect_codex_binary_version", return_value=("codex-cli 0.148.0", None)):
            with patch("codex_compat.evaluate_native_delete_safety", return_value={"supported": False, "reason": REASON_UNVALIDATED_VERSION}):
                with patch("codex_compat.detect_installed_package_versions", return_value={"openai-codex": "0.148.0", "openai-codex-cli-bin": "0.148.0"}):
                    report = assess_codex_compatibility(use_cache=False)
                    self.assertEqual(report.core_state, CompatibilityState.UNVERIFIED)
                    self.assertEqual(report.overall_state, CompatibilityState.UNVERIFIED)
                    self.assertFalse(report.package_mismatch)

    def test_known_feature_degradation_preserves_core_runtime(self):
        """G. When native delete is degraded (schema v42+), core remains SUPPORTED while overall is SUPPORTED_WITH_DEGRADATION."""
        with patch("codex_compat.detect_codex_binary_version", return_value=("codex-cli 0.144.4", None)):
            with patch("codex_compat.evaluate_native_delete_safety", return_value={"supported": False, "reason": REASON_POST42_INCOMPATIBLE}):
                with patch("codex_compat.detect_installed_package_versions", return_value={"openai-codex": "0.144.4", "openai-codex-cli-bin": "0.144.4"}):
                    report = assess_codex_compatibility(use_cache=False)
                    self.assertEqual(report.core_state, CompatibilityState.SUPPORTED)
                    self.assertEqual(report.overall_state, CompatibilityState.SUPPORTED_WITH_DEGRADATION)
                    self.assertEqual(report.capabilities["core_app_server"], CompatibilityState.SUPPORTED)
                    self.assertEqual(report.capabilities["native_delete"], CompatibilityState.SUPPORTED_WITH_DEGRADATION)
                    self.assertFalse(report.is_fatal)

    def test_major_version_mismatch_is_incompatible(self):
        """Major version difference (e.g. 1.0.0 vs 0.144.4) is marked INCOMPATIBLE and is_fatal."""
        with patch("codex_compat.detect_codex_binary_version", return_value=("codex-cli 1.0.0", None)):
            with patch("codex_compat.evaluate_native_delete_safety", return_value={"supported": False, "reason": REASON_UNVALIDATED_VERSION}):
                report = assess_codex_compatibility(use_cache=False)
                self.assertEqual(report.core_state, CompatibilityState.INCOMPATIBLE)
                self.assertEqual(report.overall_state, CompatibilityState.INCOMPATIBLE)
                self.assertTrue(report.is_fatal)

    # =========================================================================
    # REQUIREMENTS & PACKAGE TESTS
    # =========================================================================

    def test_requirements_baseline_consistency(self):
        """H. requirements.txt pins match VALIDATED_CODEX_VERSION."""
        consistent, error = check_requirements_consistency(REPO_ROOT)
        self.assertTrue(consistent, f"Requirements inconsistency: {error}")
        self.assertIsNone(error)

    def test_package_version_detection(self):
        """Package version detection runs safely."""
        pkg_vers = detect_installed_package_versions()
        self.assertIn(VALIDATED_CODEX_PACKAGE, pkg_vers)
        self.assertIn(VALIDATED_CLI_BIN_PACKAGE, pkg_vers)

    # =========================================================================
    # SUBPROCESS & BINARY DETECTION TESTS
    # =========================================================================

    def test_missing_binary_detection(self):
        """I. Missing binary produces clean error without exception."""
        non_existent = Path(tempfile.gettempdir()) / "non_existent_codex_binary.exe"
        raw_ver, err = detect_codex_binary_version(non_existent, use_cache=False)
        self.assertIsNone(raw_ver)
        self.assertIsNotNone(err)
        self.assertIn("not found", err.lower())

    def test_timeout_handling(self):
        """J. Subprocess timeout is caught and returned gracefully."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="codex --version", timeout=0.1)):
            with patch.object(Path, "exists", return_value=True):
                raw_ver, err = detect_codex_binary_version(Path("dummy_codex.exe"), timeout=0.1, use_cache=False)
                self.assertIsNone(raw_ver)
                self.assertIsNotNone(err)
                self.assertIn("Timeout", err)

    def test_nonzero_version_exit_handling(self):
        """K. Non-zero exit code from --version is handled safely."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "error: unknown flag"
        with patch("subprocess.run", return_value=mock_proc):
            with patch.object(Path, "exists", return_value=True):
                raw_ver, err = detect_codex_binary_version(Path("dummy_codex.exe"), use_cache=False)
                self.assertEqual(err, "exit=1")
                self.assertEqual(raw_ver, "error: unknown flag")

    def test_binary_version_caching(self):
        """N. Binary version detection caches result for same path."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "codex-cli 0.144.4"
        mock_proc.stderr = ""
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            with patch.object(Path, "exists", return_value=True):
                path = Path("cached_codex.exe")
                res1 = detect_codex_binary_version(path, use_cache=True)
                res2 = detect_codex_binary_version(path, use_cache=True)
                self.assertEqual(res1, res2)
                self.assertEqual(mock_run.call_count, 1)

    # =========================================================================
    # DOCTOR INTEGRATION TESTS
    # =========================================================================

    def test_doctor_severity_mapping(self):
        """L. Doctor summary correctly maps fatal vs degraded conditions."""
        # 1. Normal supported case
        with patch("codex_compat.detect_codex_binary_version", return_value=("codex-cli 0.144.4", None)):
            with patch("codex_compat.evaluate_native_delete_safety", return_value={"supported": True, "reason": REASON_PRE42_COMPATIBLE}):
                with patch("codex_compat.detect_installed_package_versions", return_value={"openai-codex": "0.144.4", "openai-codex-cli-bin": "0.144.4"}):
                    summary = generate_doctor_compatibility_summary()
                    self.assertEqual(summary["core_compatibility"], "SUPPORTED")
                    self.assertEqual(summary["native_delete"], "SAFE")
                    self.assertFalse(summary["package_mismatch"])
                    self.assertFalse(summary["is_fatal"])

        # 2. Degraded delete case (non-fatal)
        with patch("codex_compat.detect_codex_binary_version", return_value=("codex-cli 0.144.4", None)):
            with patch("codex_compat.evaluate_native_delete_safety", return_value={"supported": False, "reason": REASON_POST42_INCOMPATIBLE}):
                with patch("codex_compat.detect_installed_package_versions", return_value={"openai-codex": "0.144.4", "openai-codex-cli-bin": "0.144.4"}):
                    summary = generate_doctor_compatibility_summary()
                    self.assertEqual(summary["core_compatibility"], "SUPPORTED")
                    self.assertEqual(summary["native_delete"], "DEGRADED")
                    self.assertFalse(summary["is_fatal"])

        # 3. Missing binary case (fatal)
        with patch("codex_compat.detect_codex_binary_version", return_value=(None, "Executable not found: dummy")):
            summary = generate_doctor_compatibility_summary()
            self.assertEqual(summary["core_compatibility"], "INCOMPATIBLE")
            self.assertTrue(summary["is_fatal"])

        # 4. Package mismatch case (warning, non-fatal)
        with patch("codex_compat.detect_codex_binary_version", return_value=("codex-cli 0.144.4", None)):
            with patch("codex_compat.evaluate_native_delete_safety", return_value={"supported": True, "reason": REASON_PRE42_COMPATIBLE}):
                with patch("codex_compat.detect_installed_package_versions", return_value={"openai-codex": "0.144.4", "openai-codex-cli-bin": "0.148.0"}):
                    summary = generate_doctor_compatibility_summary()
                    self.assertEqual(summary["overall_compatibility"], "UNVERIFIED")
                    self.assertTrue(summary["package_mismatch"])
                    self.assertEqual(summary["package_mismatch_reason"], REASON_PACKAGE_VERSION_MISMATCH)
                    self.assertFalse(summary["is_fatal"])

    # =========================================================================
    # NATIVE DELETE SAFETY & DATABASE TESTS
    # =========================================================================

    def test_native_delete_safety_pre42_compatible(self):
        """M1. Database with agent_jobs table is PRE42_COMPATIBLE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            db_path = home / "state_5.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE agent_jobs (id TEXT PRIMARY KEY)")
            conn.commit()
            conn.close()

            safety = evaluate_native_delete_safety(
                codex_home=home,
                binary_version="codex-cli 0.144.4",
                version_error=None,
            )
            self.assertTrue(safety["supported"])
            self.assertEqual(safety["reason"], REASON_PRE42_COMPATIBLE)
            self.assertTrue(safety["has_agent_jobs"])

    def test_native_delete_safety_post42_incompatible(self):
        """M2. Database without agent_jobs and with migration 42 is POST42_INCOMPATIBLE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            db_path = home / "state_5.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY, description TEXT, success INTEGER)")
            conn.execute("INSERT INTO _sqlx_migrations VALUES (42, 'drop agent jobs', 1)")
            conn.commit()
            conn.close()

            safety = evaluate_native_delete_safety(
                codex_home=home,
                binary_version="codex-cli 0.144.4",
                version_error=None,
            )
            self.assertFalse(safety["supported"])
            self.assertEqual(safety["reason"], REASON_POST42_INCOMPATIBLE)
            self.assertFalse(safety["has_agent_jobs"])

    def test_history_manager_native_delete_delegation(self):
        """P. history_manager.native_delete_compatibility delegates properly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            safety = history_manager.native_delete_compatibility(
                codex_home=home,
            )
            self.assertIn("supported", safety)
            self.assertIn("reason", safety)
            self.assertIn("binary_version", safety)
            self.assertIn("state_exists", safety)

    # =========================================================================
    # PROTOCOL CONTRACT & FRAMING TESTS
    # =========================================================================

    def test_protocol_contract_required_methods(self):
        """Q. Protocol contract contains all required methods for CX2."""
        self.assertIn("initialize", CORE_REQUIRED_METHODS)
        self.assertIn("initialized", CORE_REQUIRED_METHODS)
        self.assertIn("thread/start", CORE_REQUIRED_METHODS)
        self.assertIn("thread/resume", CORE_REQUIRED_METHODS)
        self.assertIn("turn/start", CORE_REQUIRED_METHODS)

        self.assertIn("thread/list", THREAD_MANAGEMENT_METHODS)
        self.assertIn("thread/read", THREAD_MANAGEMENT_METHODS)
        self.assertIn("thread/archive", THREAD_MANAGEMENT_METHODS)
        self.assertIn("thread/delete", THREAD_MANAGEMENT_METHODS)

        self.assertIn("item/commandExecution/requestApproval", INTERACTIVE_REQUEST_METHODS)
        self.assertIn("item/fileChange/requestApproval", INTERACTIVE_REQUEST_METHODS)
        self.assertIn("turn/interrupt", INTERACTIVE_REQUEST_METHODS)

        self.assertIn("turn/started", STREAMED_NOTIFICATION_METHODS)
        self.assertIn("turn/completed", STREAMED_NOTIFICATION_METHODS)

    def test_protocol_framing_does_not_require_strict_jsonrpc_field(self):
        """R. AppServerClient framing uses JSONL with id/method/params/result and does NOT require jsonrpc: 2.0."""
        client = AppServerClient(Path("dummy_codex.exe"))

        # Inbound notification without 'jsonrpc': '2.0' must route successfully
        client._route_message({
            "method": "turn/started",
            "params": {"turnId": "t1"}
        })
        notifications = client.drain_notifications()
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["method"], "turn/started")

        # Inbound server request without 'jsonrpc': '2.0' must route successfully
        client._route_message({
            "id": "req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "git status"}
        })
        requests = client.drain_server_requests()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["id"], "req-1")

        # Inbound response without 'jsonrpc': '2.0' must route successfully
        import queue
        waiter = queue.Queue(maxsize=1)
        with client._pending_lock:
            client._pending[42] = waiter
        client._route_message({
            "id": 42,
            "result": {"ok": True}
        })
        res = waiter.get_nowait()
        self.assertEqual(res["result"]["ok"], True)


if __name__ == "__main__":
    unittest.main()
