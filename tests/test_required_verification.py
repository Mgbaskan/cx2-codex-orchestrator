from __future__ import annotations

from pathlib import Path
import sys
from typing import Any
import unittest

# Ensure tests bootstrap and imports work
sys.path.insert(0, str(Path(__file__).parent.resolve()))
import _bootstrap  # type: ignore[import-untyped]

from required_verification import (
    GateCoverage,
    RequiredVerificationGate,
    RequiredVerificationPlan,
    VerificationCoverageAssessment,
    evaluate_required_coverage,
    extract_required_verification_plan,
    normalize_canonical_command,
    unwrap_command_and_surface,
)
from verification_gate import (
    CommandExecutionSummary,
    VerificationAssessment,
    assess_turn,
)


class TestRequiredVerification(unittest.TestCase):

    # =========================================================================
    # 1. EXTRACTION TESTS (Section 28)
    # =========================================================================

    def test_01_exact_hibrit_matrix_extraction(self) -> None:
        """
        Exact HIBRIT fixture extraction:
        Mobile/root: 2 gates
        Backend: 3 gates
        Web: 3 gates
        Total: exactly 8 unique gates.
        """
        prompt = """
# HIBRIT MONOREPO ARCHITECTURE AUDIT AND REFACTOR PLAN

Mobil, backend ve web katmanlarındaki P0-01/P0-02 maddelerini plana göre tamamla; test, lint ve build kapılarını çalıştır.

QUALITY GATES

Mobile/root:
- npx tsc --noEmit
- npx jest --runInBand

Backend:
- npm run lint
- npx jest --runInBand
- npm run build

Web:
- npm run lint
- npm run type-check
- npm run build
"""
        plan = extract_required_verification_plan(prompt)
        self.assertEqual(len(plan.gates), 8)

        # Verify surfaces
        surf_counts: dict[str, int] = {}
        for g in plan.gates:
            surf_counts[g.surface] = surf_counts.get(g.surface, 0) + 1

        self.assertEqual(surf_counts.get("Mobile/root"), 2)
        self.assertEqual(surf_counts.get("Backend"), 3)
        self.assertEqual(surf_counts.get("Web"), 3)

        # Verify categories
        cats = [g.category for g in plan.gates]
        self.assertEqual(cats.count("TYPECHECK"), 2)  # mobile tsc, web type-check
        self.assertEqual(cats.count("TEST"), 2)       # mobile jest, backend jest
        self.assertEqual(cats.count("LINT"), 2)       # backend lint, web lint
        self.assertEqual(cats.count("BUILD"), 2)      # backend build, web build

    def test_02_readme_example_removal_not_extracted(self) -> None:
        """README example removal context must yield 0 required gates."""
        prompt = "README'den `npm run build` örneğini kaldır."
        plan = extract_required_verification_plan(prompt)
        self.assertEqual(len(plan.gates), 0)

    def test_03_explain_command_not_extracted(self) -> None:
        """Explain command context must yield 0 required gates."""
        prompt = "npm run lint komutunun ne yaptığını açıkla."
        plan = extract_required_verification_plan(prompt)
        self.assertEqual(len(plan.gates), 0)

    def test_04_negated_commands_not_extracted(self) -> None:
        """Explicit negative commands must yield 0 required gates."""
        prompt = "Şu komutları çalıştırma:\nnpm run lint\nnpm run build"
        plan = extract_required_verification_plan(prompt)
        self.assertEqual(len(plan.gates), 0)

    def test_05_quality_gates_backend_two_gates(self) -> None:
        """Explicit quality gates header with Backend surface yields 2 gates."""
        prompt = "Quality gates:\nBackend:\n- npm run lint\n- npm run build"
        plan = extract_required_verification_plan(prompt)
        self.assertEqual(len(plan.gates), 2)
        self.assertEqual(plan.gates[0].surface, "Backend")
        self.assertEqual(plan.gates[0].category, "LINT")
        self.assertEqual(plan.gates[1].surface, "Backend")
        self.assertEqual(plan.gates[1].category, "BUILD")

    def test_06_tests_to_run_pytest(self) -> None:
        """Tests to run with pytest yields 1 TEST gate."""
        prompt = "Tests to run:\n- pytest"
        plan = extract_required_verification_plan(prompt)
        self.assertEqual(len(plan.gates), 1)
        self.assertEqual(plan.gates[0].category, "TEST")
        self.assertEqual(plan.gates[0].normalized_command, "pytest")

    def test_07_run_go_test(self) -> None:
        """Run section with go test yields 1 TEST gate."""
        prompt = "Run:\ngo test ./..."
        plan = extract_required_verification_plan(prompt)
        self.assertEqual(len(plan.gates), 1)
        self.assertEqual(plan.gates[0].category, "TEST")
        self.assertEqual(plan.gates[0].normalized_command, "go-test:./...")

    def test_08_unsafe_publish_deploy_filtered(self) -> None:
        """Unsafe deployment/publish/destructive commands are not extracted as quality gates."""
        prompt = "Deploy:\nnpm publish\ngit reset --hard"
        plan = extract_required_verification_plan(prompt)
        self.assertEqual(len(plan.gates), 0)

    def test_09_fenced_code_block_without_gate_header_not_extracted(self) -> None:
        """Markdown code block without verification section header is not extracted."""
        prompt = "Here is an example script:\n```bash\nnpm test\n```\nExplain how it works."
        plan = extract_required_verification_plan(prompt)
        self.assertEqual(len(plan.gates), 0)

    def test_10_turkish_zorunlu_dogrulama(self) -> None:
        """Turkish 'Zorunlu doğrulama' header yields 1 LINT gate."""
        prompt = "Zorunlu doğrulama:\n- npm run lint"
        plan = extract_required_verification_plan(prompt)
        self.assertEqual(len(plan.gates), 1)
        self.assertEqual(plan.gates[0].category, "LINT")
        self.assertEqual(plan.gates[0].normalized_command, "npm-script:lint")

    def test_11_kalite_kapilari_header(self) -> None:
        """Turkish 'KALİTE KAPILARI' header yields cargo test gate."""
        prompt = "KALİTE KAPILARI:\n- cargo test"
        plan = extract_required_verification_plan(prompt)
        self.assertEqual(len(plan.gates), 1)
        self.assertEqual(plan.gates[0].category, "TEST")
        self.assertEqual(plan.gates[0].normalized_command, "cargo-test")

    def test_12_empty_or_whitespace_prompt(self) -> None:
        """Empty or whitespace prompt yields empty plan."""
        self.assertEqual(len(extract_required_verification_plan("").gates), 0)
        self.assertEqual(len(extract_required_verification_plan("   \n\t").gates), 0)

    # =========================================================================
    # 2. COMMAND NORMALIZATION & IDENTITY (Section 14 & 15)
    # =========================================================================

    def test_13_npm_script_normalizations(self) -> None:
        """npm run scripts normalize to canonical identities."""
        self.assertEqual(normalize_canonical_command("npm run type-check"), "npm-script:typecheck")
        self.assertEqual(normalize_canonical_command("npm run typecheck"), "npm-script:typecheck")
        self.assertEqual(normalize_canonical_command("npm run build"), "npm-script:build")
        self.assertEqual(normalize_canonical_command("npm run lint"), "npm-script:lint")

    def test_14_jest_flag_normalization(self) -> None:
        """Jest with flags normalizes accurately."""
        self.assertEqual(normalize_canonical_command("npx jest --runInBand"), "jest:--runinband")
        self.assertEqual(normalize_canonical_command("jest -i"), "jest:--runinband")
        self.assertEqual(normalize_canonical_command("npx jest"), "jest")

    def test_15_tsc_noemit_normalization(self) -> None:
        """TSC with --noEmit normalizes to tsc:--noemit."""
        self.assertEqual(normalize_canonical_command("npx tsc --noEmit"), "tsc:--noemit")
        self.assertEqual(normalize_canonical_command("tsc --noEmit"), "tsc:--noemit")
        self.assertEqual(normalize_canonical_command("npx tsc"), "tsc")

    def test_16_script_arg_distinction(self) -> None:
        """npm run build does NOT match npm run build:docs."""
        self.assertNotEqual(
            normalize_canonical_command("npm run build"),
            normalize_canonical_command("npm run build:docs"),
        )

    def test_17_test_runner_distinction(self) -> None:
        """npm test does NOT equal npx jest --runInBand."""
        self.assertNotEqual(
            normalize_canonical_command("npm test"),
            normalize_canonical_command("npx jest --runInBand"),
        )

    # =========================================================================
    # 3. MATCHING & SURFACE ISOLATION (Section 29)
    # =========================================================================

    def test_18_backend_npm_build_matches_backend_gate(self) -> None:
        """Backend npm run build matches backend required build gate."""
        plan = extract_required_verification_plan("Quality gates:\nBackend:\n- npm run build")
        cmds = [{"command": "npm run build", "exit_code": 0, "categories": ["BUILD"], "sequence": 1, "cwd": "backend"}]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "ALL_PASSED")
        self.assertEqual(cov.passed_count, 1)

    def test_19_web_npm_build_does_not_satisfy_backend_gate(self) -> None:
        """Web npm run build does NOT satisfy Backend npm run build gate."""
        plan = extract_required_verification_plan("Quality gates:\nBackend:\n- npm run build")
        cmds = [{"command": "cd web && npm run build", "exit_code": 0, "categories": ["BUILD"], "sequence": 1}]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "UNVERIFIED")
        self.assertEqual(cov.missing_count, 1)
        self.assertEqual(cov.passed_count, 0)

    def test_20_cd_backend_and_npm_build_matches_backend_gate(self) -> None:
        """cd backend && npm run build matches backend gate via wrapper unwrapping."""
        plan = extract_required_verification_plan("Quality gates:\nBackend:\n- npm run build")
        cmds = [{"command": "cd backend && npm run build", "exit_code": 0, "categories": ["BUILD"], "sequence": 1}]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "ALL_PASSED")
        self.assertEqual(cov.passed_count, 1)

    def test_21_npm_prefix_backend_matches_backend_gate(self) -> None:
        """npm --prefix backend run build matches backend gate."""
        plan = extract_required_verification_plan("Quality gates:\nBackend:\n- npm run build")
        cmds = [{"command": "npm --prefix backend run build", "exit_code": 0, "categories": ["BUILD"], "sequence": 1}]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "ALL_PASSED")
        self.assertEqual(cov.passed_count, 1)

    def test_22_set_location_backend_matches_backend_gate(self) -> None:
        """Set-Location backend; npm run build matches backend gate."""
        plan = extract_required_verification_plan("Quality gates:\nBackend:\n- npm run build")
        cmds = [{"command": "Set-Location backend; npm run build", "exit_code": 0, "categories": ["BUILD"], "sequence": 1}]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "ALL_PASSED")
        self.assertEqual(cov.passed_count, 1)

    def test_23_mobile_root_jest_isolated_from_backend_jest(self) -> None:
        """A single backend Jest execution satisfies only backend Jest gate, not mobile Jest gate."""
        prompt = "Quality gates:\nMobile/root:\n- npx jest --runInBand\nBackend:\n- npx jest --runInBand"
        plan = extract_required_verification_plan(prompt)
        cmds = [{"command": "cd backend && npx jest --runInBand", "exit_code": 0, "categories": ["TEST"], "sequence": 1}]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "PARTIALLY_PASSED")
        self.assertEqual(cov.passed_count, 1)
        self.assertEqual(cov.missing_count, 1)
        self.assertEqual(cov.missing_gates[0].surface, "Mobile/root")

    def test_24_duplicate_rerun_latest_chronological_wins(self) -> None:
        """If a gate fails first and passes on rerun, latest chronological execution wins (PASSED)."""
        plan = extract_required_verification_plan("Quality gates:\nBackend:\n- npm run lint")
        cmds = [
            {"command": "cd backend && npm run lint", "exit_code": 1, "categories": ["LINT"], "sequence": 1, "classification_text": "1 error"},
            {"command": "cd backend && npm run lint", "exit_code": 0, "categories": ["LINT"], "sequence": 2},
        ]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "ALL_PASSED")
        self.assertEqual(cov.passed_count, 1)
        self.assertEqual(cov.failed_count, 0)

    def test_25_duplicate_rerun_regression_latest_fails(self) -> None:
        """If a gate passes first and fails on rerun, latest chronological execution wins (FAILED)."""
        plan = extract_required_verification_plan("Quality gates:\nBackend:\n- npm run lint")
        cmds = [
            {"command": "cd backend && npm run lint", "exit_code": 0, "categories": ["LINT"], "sequence": 1},
            {"command": "cd backend && npm run lint", "exit_code": 1, "categories": ["LINT"], "sequence": 2, "classification_text": "eslint: error in index.ts"},
        ]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "FAILED")
        self.assertEqual(cov.passed_count, 0)
        self.assertEqual(cov.failed_count, 1)

    # =========================================================================
    # 4. COVERAGE STATUS & PRECEDENCE (Section 30)
    # =========================================================================

    def test_26_all_8_required_passed(self) -> None:
        """8 required / 8 pass -> ALL_PASSED."""
        plan = extract_required_verification_plan("""
QUALITY GATES
Mobile/root:
- npx tsc --noEmit
- npx jest --runInBand
Backend:
- npm run lint
- npx jest --runInBand
- npm run build
Web:
- npm run lint
- npm run type-check
- npm run build
""")
        cmds = [
            {"command": "npx tsc --noEmit", "exit_code": 0, "categories": ["TYPECHECK"], "sequence": 1, "cwd": "."},
            {"command": "npx jest --runInBand", "exit_code": 0, "categories": ["TEST"], "sequence": 2, "cwd": "."},
            {"command": "cd backend && npm run lint", "exit_code": 0, "categories": ["LINT"], "sequence": 3},
            {"command": "cd backend && npx jest --runInBand", "exit_code": 0, "categories": ["TEST"], "sequence": 4},
            {"command": "npm --prefix backend run build", "exit_code": 0, "categories": ["BUILD"], "sequence": 5},
            {"command": "cd web && npm run lint", "exit_code": 0, "categories": ["LINT"], "sequence": 6},
            {"command": "cd web && npm run type-check", "exit_code": 0, "categories": ["TYPECHECK"], "sequence": 7},
            {"command": "cd web && npm run build", "exit_code": 0, "categories": ["BUILD"], "sequence": 8},
        ]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "ALL_PASSED")
        self.assertEqual(cov.passed_count, 8)
        self.assertEqual(cov.missing_count, 0)

    def test_27_7_passed_1_missing_incomplete(self) -> None:
        """8 required / 7 pass / 1 missing -> PARTIALLY_PASSED."""
        plan = extract_required_verification_plan("""
QUALITY GATES
Mobile/root:
- npx tsc --noEmit
- npx jest --runInBand
Backend:
- npm run lint
- npx jest --runInBand
- npm run build
Web:
- npm run lint
- npm run type-check
- npm run build
""")
        cmds = [
            {"command": "npx tsc --noEmit", "exit_code": 0, "categories": ["TYPECHECK"], "sequence": 1, "cwd": "."},
            {"command": "npx jest --runInBand", "exit_code": 0, "categories": ["TEST"], "sequence": 2, "cwd": "."},
            {"command": "cd backend && npm run lint", "exit_code": 0, "categories": ["LINT"], "sequence": 3},
            {"command": "cd backend && npx jest --runInBand", "exit_code": 0, "categories": ["TEST"], "sequence": 4},
            {"command": "npm --prefix backend run build", "exit_code": 0, "categories": ["BUILD"], "sequence": 5},
            {"command": "cd web && npm run lint", "exit_code": 0, "categories": ["LINT"], "sequence": 6},
            {"command": "cd web && npm run build", "exit_code": 0, "categories": ["BUILD"], "sequence": 7},
        ]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "PARTIALLY_PASSED")
        self.assertEqual(cov.passed_count, 7)
        self.assertEqual(cov.missing_count, 1)
        self.assertEqual(cov.missing_gates[0].surface, "Web")

    def test_28_7_passed_1_failed_is_failed(self) -> None:
        """8 required / 7 pass / 1 fail -> FAILED."""
        plan = extract_required_verification_plan("Quality gates:\nBackend:\n- npm run lint\n- npm run build")
        cmds = [
            {"command": "cd backend && npm run lint", "exit_code": 0, "categories": ["LINT"], "sequence": 1},
            {"command": "cd backend && npm run build", "exit_code": 1, "categories": ["BUILD"], "sequence": 2, "classification_text": "SyntaxError: Unexpected token"},
        ]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "FAILED")
        self.assertEqual(cov.passed_count, 1)
        self.assertEqual(cov.failed_count, 1)

    def test_29_7_passed_1_blocked_is_blocked(self) -> None:
        """8 required / 7 pass / 1 blocked -> BLOCKED."""
        plan = extract_required_verification_plan("Quality gates:\nBackend:\n- npm run lint\n- npm run build")
        cmds = [
            {"command": "cd backend && npm run lint", "exit_code": 0, "categories": ["LINT"], "sequence": 1},
            {"command": "cd backend && npm run build", "exit_code": 1, "categories": ["BUILD"], "sequence": 2, "classification_text": "permission denied"},
        ]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "BLOCKED")
        self.assertEqual(cov.blocked_count, 1)

    def test_30_7_passed_1_inconclusive_is_inconclusive(self) -> None:
        """8 required / 7 pass / 1 inconclusive -> INCONCLUSIVE."""
        plan = extract_required_verification_plan("Quality gates:\nBackend:\n- npm run lint\n- npm run build")
        cmds = [
            {"command": "cd backend && npm run lint", "exit_code": 0, "categories": ["LINT"], "sequence": 1},
            {"command": "cd backend && npm run build", "exit_code": 1, "categories": ["BUILD"], "sequence": 2, "is_masked": True},
        ]
        cov = evaluate_required_coverage(plan, cmds)
        self.assertEqual(cov.status, "INCONCLUSIVE")
        self.assertEqual(cov.inconclusive_count, 1)

    def test_31_interrupted_is_interrupted(self) -> None:
        """Turn interrupted -> INTERRUPTED."""
        plan = extract_required_verification_plan("Quality gates:\nBackend:\n- npm run lint")
        cov = evaluate_required_coverage(plan, [], is_interrupted=True)
        self.assertEqual(cov.status, "INTERRUPTED")

    def test_32_zero_observed_is_unverified(self) -> None:
        """8 required / 0 observed -> UNVERIFIED."""
        plan = extract_required_verification_plan("Quality gates:\nBackend:\n- npm run lint")
        cov = evaluate_required_coverage(plan, [])
        self.assertEqual(cov.status, "UNVERIFIED")
        self.assertEqual(cov.missing_count, 1)

    def test_33_no_required_plan_backward_compatibility(self) -> None:
        """Empty plan -> ALL_PASSED (0 total)."""
        plan = RequiredVerificationPlan(gates=())
        cov = evaluate_required_coverage(plan, [])
        self.assertEqual(cov.status, "ALL_PASSED")
        self.assertEqual(cov.required_total, 0)

    # =========================================================================
    # 5. ASSURANCE INTEGRATION & MODEL PROSE NON-AUTHORITY (Section 20, 26, 31)
    # =========================================================================

    def test_34_hibrit_synthetic_7_of_8_ledger(self) -> None:
        """
        Synthetic 7/8 execution:
        Even if mutation test passed on backend, missing 1 gate prevents VERIFIED status.
        Result must be PARTIALLY_VERIFIED, NOT VERIFIED.
        """
        hibrit_plan = extract_required_verification_plan("""
QUALITY GATES
Mobile/root:
- npx tsc --noEmit
- npx jest --runInBand
Backend:
- npm run lint
- npx jest --runInBand
- npm run build
Web:
- npm run lint
- npm run type-check
- npm run build
""")
        cmds = [
            CommandExecutionSummary(command="npx tsc --noEmit", exit_code=0, categories=["TYPECHECK"], sequence=10),
            CommandExecutionSummary(command="npx jest --runInBand", exit_code=0, categories=["TEST"], sequence=11),
            CommandExecutionSummary(command="cd backend && npm run lint", exit_code=0, categories=["LINT"], sequence=12),
            CommandExecutionSummary(command="cd backend && npx jest --runInBand", exit_code=0, categories=["TEST"], sequence=13),
            CommandExecutionSummary(command="npm --prefix backend run build", exit_code=0, categories=["BUILD"], sequence=14),
            CommandExecutionSummary(command="cd web && npm run lint", exit_code=0, categories=["LINT"], sequence=15),
            CommandExecutionSummary(command="cd web && npm run build", exit_code=0, categories=["BUILD"], sequence=16),
        ]
        # Changed files on backend
        assessment = assess_turn(
            changed_files=["backend/src/service.ts"],
            command_executions=cmds,
            last_mutation_seq=5,
            required_plan=hibrit_plan,
        )

        self.assertEqual(assessment.status, "PARTIALLY_VERIFIED")
        self.assertEqual(assessment.reason, "REQUIRED_GATES_INCOMPLETE")
        self.assertIsNotNone(assessment.required_coverage)
        self.assertEqual(assessment.required_coverage.passed_count, 7)
        self.assertEqual(assessment.required_coverage.missing_count, 1)

    def test_35_hibrit_synthetic_8_of_8_ledger(self) -> None:
        """
        Synthetic 8/8 execution:
        All 8 gates observed and passed -> assessment is VERIFIED.
        """
        hibrit_plan = extract_required_verification_plan("""
QUALITY GATES
Mobile/root:
- npx tsc --noEmit
- npx jest --runInBand
Backend:
- npm run lint
- npx jest --runInBand
- npm run build
Web:
- npm run lint
- npm run type-check
- npm run build
""")
        cmds = [
            CommandExecutionSummary(command="npx tsc --noEmit", exit_code=0, categories=["TYPECHECK"], sequence=10),
            CommandExecutionSummary(command="npx jest --runInBand", exit_code=0, categories=["TEST"], sequence=11),
            CommandExecutionSummary(command="cd backend && npm run lint", exit_code=0, categories=["LINT"], sequence=12),
            CommandExecutionSummary(command="cd backend && npx jest --runInBand", exit_code=0, categories=["TEST"], sequence=13),
            CommandExecutionSummary(command="npm --prefix backend run build", exit_code=0, categories=["BUILD"], sequence=14),
            CommandExecutionSummary(command="cd web && npm run lint", exit_code=0, categories=["LINT"], sequence=15),
            CommandExecutionSummary(command="cd web && npm run type-check", exit_code=0, categories=["TYPECHECK"], sequence=16),
            CommandExecutionSummary(command="cd web && npm run build", exit_code=0, categories=["BUILD"], sequence=17),
        ]
        assessment = assess_turn(
            changed_files=["backend/src/service.ts"],
            command_executions=cmds,
            last_mutation_seq=5,
            required_plan=hibrit_plan,
        )

        self.assertEqual(assessment.status, "VERIFIED")
        self.assertEqual(assessment.required_coverage.passed_count, 8)
        self.assertEqual(assessment.required_coverage.missing_count, 0)

    def test_36_model_prose_cannot_fake_coverage(self) -> None:
        """
        Synthetic model final text 'All tests passed successfully' has ZERO authority.
        If only 1 of 8 required commands was run:
        Coverage is PARTIALLY_PASSED (1 passed, 7 missing).
        Final assurance is UNVERIFIED (NOT VERIFIED).
        """
        hibrit_plan = extract_required_verification_plan("""
QUALITY GATES
Mobile/root:
- npx tsc --noEmit
- npx jest --runInBand
Backend:
- npm run lint
- npx jest --runInBand
- npm run build
Web:
- npm run lint
- npm run type-check
- npm run build
""")
        # Only 1 command executed in reality
        cmds = [
            CommandExecutionSummary(command="cd backend && npm run build", exit_code=0, categories=["BUILD"], sequence=10),
        ]
        assessment = assess_turn(
            changed_files=["backend/src/service.ts"],
            command_executions=cmds,
            last_mutation_seq=5,
            required_plan=hibrit_plan,
        )

        self.assertNotEqual(assessment.status, "VERIFIED")
        self.assertEqual(assessment.status, "UNVERIFIED")
        self.assertIsNotNone(assessment.required_coverage)
        self.assertEqual(assessment.required_coverage.status, "PARTIALLY_PASSED")
        self.assertEqual(assessment.required_coverage.passed_count, 1)
        self.assertEqual(assessment.required_coverage.missing_count, 7)

    def test_37_read_only_task_with_required_test_gate_satisfied(self) -> None:
        """
        Read-only task with explicit required verification gate:
        If required gate is observed and passed, assurance status remains NOT_APPLICABLE (DO NOT UPGRADE).
        Required coverage is ALL_PASSED.
        """
        plan = extract_required_verification_plan("Do not modify files.\nQuality gates:\n- npm test")
        cmds = [
            CommandExecutionSummary(command="npm test", exit_code=0, categories=["TEST"], sequence=1),
        ]
        assessment = assess_turn(
            changed_files=[],
            command_executions=cmds,
            last_mutation_seq=0,
            required_plan=plan,
        )
        self.assertEqual(assessment.status, "NOT_APPLICABLE")
        self.assertEqual(assessment.reason, "NO_MUTATION")
        self.assertIsNotNone(assessment.required_coverage)
        self.assertEqual(assessment.required_coverage.status, "ALL_PASSED")
        self.assertEqual(assessment.required_coverage.passed_count, 1)

    def test_38_read_only_task_with_failed_test_gate(self) -> None:
        """
        Read-only task with explicit required verification gate:
        If required gate fails (e.g. test failure report), assurance status is FAILED (conservative downgrade).
        Required coverage is FAILED.
        """
        plan = extract_required_verification_plan("Do not modify files.\nQuality gates:\n- npm test")
        cmds = [
            CommandExecutionSummary(command="npm test", exit_code=1, categories=["TEST"], sequence=1, classification_text="FAIL tests/index.test.ts 1 failed"),
        ]
        assessment = assess_turn(
            changed_files=[],
            command_executions=cmds,
            last_mutation_seq=0,
            required_plan=plan,
        )
        self.assertEqual(assessment.status, "FAILED")
        self.assertEqual(assessment.reason, "REQUIRED_GATE_FAILED")
        self.assertEqual(assessment.required_coverage.status, "FAILED")

    def test_39_read_only_task_with_blocked_test_gate(self) -> None:
        """
        Read-only task with explicit required verification gate:
        If required gate is blocked (e.g. permission denied), assurance status is BLOCKED.
        Required coverage is BLOCKED.
        """
        plan = extract_required_verification_plan("Do not modify files.\nQuality gates:\n- npm test")
        cmds = [
            CommandExecutionSummary(command="npm test", exit_code=1, categories=["TEST"], sequence=1, output_snippet="permission denied"),
        ]
        assessment = assess_turn(
            changed_files=[],
            command_executions=cmds,
            last_mutation_seq=0,
            required_plan=plan,
        )
        self.assertEqual(assessment.status, "BLOCKED")
        self.assertEqual(assessment.reason, "REQUIRED_GATE_BLOCKED")
        self.assertEqual(assessment.required_coverage.status, "BLOCKED")

    def test_40_coverage_one_of_eight_is_partially_passed(self) -> None:
        """1 passed out of 8 required gates -> coverage status is PARTIALLY_PASSED."""
        hibrit_plan = extract_required_verification_plan("""
QUALITY GATES
Mobile/root:
- npx tsc --noEmit
- npx jest --runInBand
Backend:
- npm run lint
- npx jest --runInBand
- npm run build
Web:
- npm run lint
- npm run type-check
- npm run build
""")
        cmds = [
            {"command": "cd backend && npm run build", "exit_code": 0, "categories": ["BUILD"], "sequence": 1},
        ]
        cov = evaluate_required_coverage(hibrit_plan, cmds)
        self.assertEqual(cov.status, "PARTIALLY_PASSED")
        self.assertEqual(cov.passed_count, 1)
        self.assertEqual(cov.missing_count, 7)

    def test_41_base_unverified_plus_all_passed_remains_unverified(self) -> None:
        """Base UNVERIFIED + ALL_PASSED required coverage -> remains UNVERIFIED."""
        plan = extract_required_verification_plan("Quality gates:\n- npm run lint")
        cmds = [
            CommandExecutionSummary(command="npm run lint", exit_code=0, categories=["LINT"], sequence=1),
        ]
        # user_skip yields base UNVERIFIED
        assessment = assess_turn(
            changed_files=["src/app.ts"],
            command_executions=cmds,
            last_mutation_seq=0,
            user_skip=True,
            required_plan=plan,
        )
        self.assertEqual(assessment.status, "UNVERIFIED")
        self.assertEqual(assessment.reason, "USER_REQUESTED_SKIP")
        self.assertEqual(assessment.required_coverage.status, "ALL_PASSED")

    def test_42_base_partially_verified_plus_all_passed_remains_partially_verified(self) -> None:
        """Base PARTIALLY_VERIFIED + ALL_PASSED required coverage -> remains PARTIALLY_VERIFIED."""
        plan = extract_required_verification_plan("Quality gates:\n- npm run build")
        cmds = [
            CommandExecutionSummary(command="npm run build", exit_code=0, categories=["BUILD"], sequence=2),
        ]
        # Mutation on package.json (CONFIG_BUILD) + build pass => base PARTIALLY_VERIFIED
        assessment = assess_turn(
            changed_files=["package.json"],
            command_executions=cmds,
            last_mutation_seq=1,
            required_plan=plan,
        )
        self.assertEqual(assessment.status, "PARTIALLY_VERIFIED")
        self.assertEqual(assessment.required_coverage.status, "ALL_PASSED")

    def test_43_base_failed_plus_all_passed_remains_failed(self) -> None:
        """Base FAILED (from mutation test failure) + ALL_PASSED required coverage -> remains FAILED."""
        plan = extract_required_verification_plan("Quality gates:\n- npm run lint")
        cmds = [
            CommandExecutionSummary(command="npm run lint", exit_code=0, categories=["LINT"], sequence=2),
            CommandExecutionSummary(command="npm test", exit_code=1, categories=["TEST"], sequence=3, classification_text="FAIL test.ts 1 failed"),
        ]
        assessment = assess_turn(
            changed_files=["src/app.ts"],
            command_executions=cmds,
            last_mutation_seq=1,
            is_continuation=True,
            required_plan=plan,
        )
        self.assertEqual(assessment.status, "FAILED")
        self.assertEqual(assessment.required_coverage.status, "ALL_PASSED")

    def test_44_base_not_applicable_plus_all_passed_remains_not_applicable(self) -> None:
        """Base NOT_APPLICABLE (docs only) + ALL_PASSED required coverage -> remains NOT_APPLICABLE."""
        plan = extract_required_verification_plan("Quality gates:\n- npm run lint")
        cmds = [
            CommandExecutionSummary(command="npm run lint", exit_code=0, categories=["LINT"], sequence=2),
        ]
        assessment = assess_turn(
            changed_files=["README.md"],
            command_executions=cmds,
            last_mutation_seq=1,
            required_plan=plan,
        )
        self.assertEqual(assessment.status, "NOT_APPLICABLE")
        self.assertEqual(assessment.reason, "DOCS_ONLY_MUTATION")
        self.assertEqual(assessment.required_coverage.status, "ALL_PASSED")


if __name__ == "__main__":
    unittest.main()
