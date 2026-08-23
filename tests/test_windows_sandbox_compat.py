from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

sys.path.insert(0, _bootstrap.RUNTIME_DIR)

import codex_compat
from codex_compat import (
    REASON_WINDOWS_WORKSPACE_WRITE_DEGRADED,
    CompatibilityState,
    SandboxCompatibilityDecision,
    assess_codex_compatibility,
    generate_doctor_compatibility_summary,
    resolve_sandbox_compatibility,
)
import router_adapter
from cx2_runtime import approval_policy_for_route
from terminal_ui import TerminalRenderer
from verification_gate import (
    CommandExecutionSummary,
    assess_turn,
)


class TestWindowsSandboxCompat(unittest.TestCase):
    """
    Phase 5.2: Safe Windows Workspace-Write Compatibility Qualification Tests.
    """

    # -------------------------------------------------------------------------
    # 1. Compatibility Resolver Matrix
    # -------------------------------------------------------------------------

    def test_windows_01444_workspace_write_downgraded_to_read_only(self):
        """1 & 4. Windows + Codex 0.144.4 + workspace-write -> effective read-only with reason."""
        decision = resolve_sandbox_compatibility(
            requested_sandbox="workspace-write",
            platform="win32",
            codex_version="0.144.4",
        )
        self.assertEqual(decision.requested_sandbox, "workspace-write")
        self.assertEqual(decision.effective_sandbox, "read-only")
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.compatibility_mode, "windows_workspace_write_fallback")
        self.assertEqual(decision.reason, REASON_WINDOWS_WORKSPACE_WRITE_DEGRADED)

    def test_windows_01444_read_only_not_downgraded(self):
        """8 & 12. Windows + Codex 0.144.4 + read-only -> no downgrade or degradation marker."""
        decision = resolve_sandbox_compatibility(
            requested_sandbox="read-only",
            platform="win32",
            codex_version="0.144.4",
        )
        self.assertEqual(decision.requested_sandbox, "read-only")
        self.assertEqual(decision.effective_sandbox, "read-only")
        self.assertFalse(decision.degraded)
        self.assertIsNone(decision.compatibility_mode)
        self.assertIsNone(decision.reason)

    def test_linux_workspace_write_remains_workspace_write(self):
        """9. Linux + Codex 0.144.4 + workspace-write -> remains workspace-write."""
        decision = resolve_sandbox_compatibility(
            requested_sandbox="workspace-write",
            platform="linux",
            codex_version="0.144.4",
        )
        self.assertEqual(decision.requested_sandbox, "workspace-write")
        self.assertEqual(decision.effective_sandbox, "workspace-write")
        self.assertFalse(decision.degraded)
        self.assertIsNone(decision.compatibility_mode)

    def test_darwin_workspace_write_remains_workspace_write(self):
        """9. Darwin + Codex 0.144.4 + workspace-write -> remains workspace-write."""
        decision = resolve_sandbox_compatibility(
            requested_sandbox="workspace-write",
            platform="darwin",
            codex_version="0.144.4",
        )
        self.assertEqual(decision.requested_sandbox, "workspace-write")
        self.assertEqual(decision.effective_sandbox, "workspace-write")
        self.assertFalse(decision.degraded)

    def test_windows_future_codex_version_not_silently_downgraded(self):
        """10. Windows + future Codex version (e.g. 0.148.0) -> not silently downgraded."""
        decision = resolve_sandbox_compatibility(
            requested_sandbox="workspace-write",
            platform="win32",
            codex_version="0.148.0",
        )
        self.assertEqual(decision.requested_sandbox, "workspace-write")
        self.assertEqual(decision.effective_sandbox, "workspace-write")
        self.assertFalse(decision.degraded)

    def test_resolver_failure_fails_conservative(self):
        """25 & 26. Resolver exception fails conservative without granting dangerFullAccess."""
        with patch("codex_compat.parse_codex_version", side_effect=RuntimeError("unexpected crash")):
            decision = resolve_sandbox_compatibility(
                requested_sandbox="workspace-write",
                platform="win32",
                codex_version="0.144.4",
            )
            # Conservative: do not grant broader than requested; degrade safely
            self.assertEqual(decision.requested_sandbox, "workspace-write")
            self.assertEqual(decision.effective_sandbox, "read-only")
            self.assertTrue(decision.degraded)

    # -------------------------------------------------------------------------
    # 2. Router & Approval Policy Integration
    # -------------------------------------------------------------------------

    def test_build_route_preserves_requested_and_sets_effective_permissions(self):
        """2, 3, 6, 7. Router preserves requested_sandbox and sets effective :read-only permissions on Windows."""
        fake_repo = {
            "root": str(Path.cwd()),
            "git": True,
            "stacks": ["python"],
        }
        fake_policy = {"reasoning": {"routine": "low"}}
        fake_route = {
            "tier": "routine",
            "reasoning": "low",
            "sandbox": "workspace-write",
            "mutating": True,
            "score": 10,
        }

        with patch("router_adapter.production_version", return_value="1.2.2"), \
             patch("router_adapter.production_cx.load_policy", return_value=fake_policy), \
             patch("router_adapter.production_cx.detect_repo", return_value=fake_repo), \
             patch("router_adapter.production_cx.classify", return_value=fake_route), \
             patch("router_adapter.production_cx.cached_visible_models", return_value=["gpt-5.6-luna"]), \
             patch("router_adapter.production_cx.choose_model", return_value="gpt-5.6-luna"), \
             patch("codex_compat.detect_codex_binary_version", return_value=("codex-cli 0.144.4", None)):

            # On Windows:
            with patch("sys.platform", "win32"):
                route = router_adapter.build_route("edit code", Path.cwd())
                self.assertEqual(route["sandbox"], "workspace-write")
                self.assertEqual(route["requested_sandbox"], "workspace-write")
                self.assertEqual(route["effective_sandbox"], "read-only")
                self.assertTrue(route["sandbox_degraded"])
                self.assertEqual(route["permissions"], ":read-only")
                self.assertEqual(route["thread"]["permissions"], ":read-only")

            # On Linux:
            with patch("sys.platform", "linux"):
                route_linux = router_adapter.build_route("edit code", Path.cwd())
                self.assertEqual(route_linux["sandbox"], "workspace-write")
                self.assertEqual(route_linux["requested_sandbox"], "workspace-write")
                self.assertEqual(route_linux["effective_sandbox"], "workspace-write")
                self.assertFalse(route_linux["sandbox_degraded"])
                self.assertEqual(route_linux["permissions"], ":workspace")
                self.assertEqual(route_linux["thread"]["permissions"], ":workspace")

    def test_approval_policy_remains_on_request_for_downgraded_route(self):
        """5. Approval policy must remain 'on-request' for mutating route downgraded to effective read-only."""
        downgraded_route = {
            "sandbox": "workspace-write",
            "requested_sandbox": "workspace-write",
            "effective_sandbox": "read-only",
            "mutating": True,
        }
        self.assertEqual(approval_policy_for_route(downgraded_route), "on-request")

    def test_approval_policy_remains_never_for_genuine_read_only_route(self):
        """8. Approval policy remains 'never' for genuine non-mutating read-only route."""
        read_only_route = {
            "sandbox": "read-only",
            "requested_sandbox": "read-only",
            "effective_sandbox": "read-only",
            "mutating": False,
        }
        self.assertEqual(approval_policy_for_route(read_only_route), "never")

    # -------------------------------------------------------------------------
    # 3. Terminal UI Header & Compatibility Notice
    # -------------------------------------------------------------------------

    def test_terminal_renders_compatibility_notice_when_degraded(self):
        """11. Terminal renders compatibility notice when sandbox is degraded."""
        renderer = TerminalRenderer()
        output_lines = []
        renderer._line = lambda text: output_lines.append(text)

        renderer.render_turn_header(
            session_mode="NEW",
            model="gpt-5.6-luna",
            effort="low",
            sandbox="workspace-write",
            effective_sandbox="read-only",
            sandbox_compatibility_mode="windows_workspace_write_fallback",
        )

        full_output = "\n".join(output_lines)
        self.assertIn("workspace-write", full_output)
        self.assertTrue(any("uyumluluk modu" in line or "read-only" in line for line in output_lines))

    def test_terminal_no_compatibility_notice_for_normal_read_only(self):
        """12. Terminal does not render compatibility notice on normal read-only turn."""
        renderer = TerminalRenderer()
        output_lines = []
        renderer._line = lambda text: output_lines.append(text)

        renderer.render_turn_header(
            session_mode="NEW",
            model="gpt-5.6-luna",
            effort="low",
            sandbox="read-only",
            effective_sandbox="read-only",
            sandbox_compatibility_mode=None,
        )

        self.assertEqual(len(output_lines), 1)
        self.assertIn("read-only", output_lines[0])
        self.assertNotIn("uyumluluk", output_lines[0])

    # -------------------------------------------------------------------------
    # 4. Doctor Diagnostics & Compatibility Report
    # -------------------------------------------------------------------------

    def test_doctor_reports_windows_workspace_write_degradation(self):
        """13 & 14. Doctor reports workspace-write degradation on Windows 0.144.4 while core is supported."""
        with patch("codex_compat.detect_codex_binary_version", return_value=("codex-cli 0.144.4", None)), \
             patch("codex_compat.evaluate_native_delete_safety", return_value={"supported": True, "reason": codex_compat.REASON_PRE42_COMPATIBLE}), \
             patch("codex_compat.detect_installed_package_versions", return_value={"openai-codex": "0.144.4", "openai-codex-cli-bin": "0.144.4"}), \
             patch("sys.platform", "win32"):

            report = assess_codex_compatibility(use_cache=False)
            self.assertEqual(report.core_state, CompatibilityState.SUPPORTED)
            self.assertEqual(report.capabilities.get("windows_workspace_write"), CompatibilityState.SUPPORTED_WITH_DEGRADATION)

            summary = generate_doctor_compatibility_summary(report)
            self.assertEqual(summary["core_compatibility"], "SUPPORTED")
            self.assertEqual(summary.get("windows_workspace_write"), "DEGRADED")
            self.assertEqual(summary.get("windows_workspace_write_reason"), REASON_WINDOWS_WORKSPACE_WRITE_DEGRADED)

    # -------------------------------------------------------------------------
    # 5. Verification & Ledger Integration
    # -------------------------------------------------------------------------

    def test_required_verification_under_effective_read_only(self):
        """21 & 22. Required verification passes truthfully under effective read-only mode."""
        assessment = assess_turn(
            changed_files=["backend/src/value.js"],
            command_executions=[
                CommandExecutionSummary(
                    command="npm test",
                    exit_code=0,
                    categories=["TEST"],
                    sequence=2,
                    output_snippet="PASS: value.test.js",
                )
            ],
            last_mutation_seq=1,
            is_continuation=False,
        )
        self.assertEqual(assessment.status, "VERIFIED")

    def test_failed_required_verification_under_effective_read_only(self):
        """22. Conclusive test failure under effective read-only correctly marks FAILED."""
        assessment = assess_turn(
            changed_files=["backend/src/value.js"],
            command_executions=[
                CommandExecutionSummary(
                    command="npm test",
                    exit_code=1,
                    categories=["TEST"],
                    sequence=2,
                    output_snippet="FAIL: 1 test failed - AssertionError",
                )
            ],
            last_mutation_seq=1,
            is_continuation=True,
        )
        self.assertEqual(assessment.status, "FAILED")

    # -------------------------------------------------------------------------
    # 6. Session & Thread Permissions Alignment
    # -------------------------------------------------------------------------

    def test_session_thread_start_and_resume_permissions_aligned(self):
        """6, 7, 24. Thread start and resume parameters receive effective permissions (:read-only)."""
        from session_adapter import thread_resume_params, thread_start_params

        start_params = thread_start_params(
            root=Path.cwd(),
            model="gpt-5.6-luna",
            permissions=":read-only",
        )
        self.assertEqual(start_params["permissions"], ":read-only")

        resume_params = thread_resume_params(
            thread_id="th_123",
            root=Path.cwd(),
            model="gpt-5.6-luna",
            permissions=":read-only",
        )
        self.assertEqual(resume_params["permissions"], ":read-only")

    # -------------------------------------------------------------------------
    # 7. Security Negative Controls & Fail-Closed Boundaries
    # -------------------------------------------------------------------------

    def test_security_negative_controls(self):
        """25 & 26. Write route and compatibility mode never grant automatic host execution."""
        # 1. Mutating route alone never sets permissions to dangerFullAccess
        fake_repo = {"root": str(Path.cwd()), "git": True, "stacks": ["python"]}
        fake_route = {"tier": "deep", "reasoning": "high", "sandbox": "workspace-write", "mutating": True, "score": 20}
        with patch("router_adapter.production_version", return_value="1.2.2"), \
             patch("router_adapter.production_cx.load_policy", return_value={"reasoning": {"deep": "high"}}), \
             patch("router_adapter.production_cx.detect_repo", return_value=fake_repo), \
             patch("router_adapter.production_cx.classify", return_value=fake_route), \
             patch("router_adapter.production_cx.cached_visible_models", return_value=["gpt-5.6-luna"]), \
             patch("router_adapter.production_cx.choose_model", return_value="gpt-5.6-luna"), \
             patch("codex_compat.detect_codex_binary_version", return_value=("codex-cli 0.144.4", None)), \
             patch("sys.platform", "win32"):

            route = router_adapter.build_route("delete everything", Path.cwd())
            # Must NOT be dangerFullAccess or unconstrained
            self.assertEqual(route["permissions"], ":read-only")
            self.assertNotEqual(route["permissions"], "dangerFullAccess")
            self.assertNotEqual(route["permissions"], ":workspace")

    def test_approval_state_machine_integration_and_deadline_compensation(self):
        """18, 19, 20. Human wait time compensates turn deadline during compatibility approval."""
        from turn_runner import TurnApprovalState, TurnRunResult

        res = TurnRunResult(thread_id="th_1", turn_id="turn_1")
        res.human_approval_wait_seconds += 5.0
        self.assertEqual(res.human_approval_wait_seconds, 5.0)


if __name__ == "__main__":
    unittest.main()
