from __future__ import annotations

import io
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

# Ensure tests bootstrap and imports work
sys.path.insert(0, str(Path(__file__).parent.resolve()))
import _bootstrap  # type: ignore[import-untyped]

import cx
from cx2_cli import build_parser, main
from prompt_transport import read_prompt_file, resolve_prompt_source

VISIBLE_MODELS = [
    {"id": "gpt-5.6-luna"},
    {"id": "gpt-5.6-terra"},
    {"id": "gpt-5.6-sol"},
]


def resolve_model(res: dict[str, Any], policy: dict[str, Any]) -> str:
    return cx.choose_model(res["tier"], VISIBLE_MODELS, policy)


class TestTaskShapeRouting(unittest.TestCase):

    def setUp(self) -> None:
        self.policy = {
            "version": 1,
            "models": {
                "routine": ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"],
                "standard": ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"],
                "deep": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
            },
            "reasoning": {
                "routine": "low",
                "standard": "medium",
                "deep": "high",
            },
            "thresholds": {
                "routine_max": 1,
                "deep_min": 7,
            },
        }
        self.dummy_repo = {
            "root": Path("C:/Users/example-user/Projects/sample-project"),
            "git": True,
            "tracked_files": 450,
            "tracked_files_bucket": "large",
            "dirty_files": 0,
            "monorepo": True,
            "stacks": ["typescript", "node", "nestjs"],
        }
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_obj.name)

    def tearDown(self) -> None:
        self.temp_dir_obj.cleanup()

    # -------------------------------------------------------------
    # 1. Sample-project Short vs Full Invariant (Sections 15 & 16)
    # -------------------------------------------------------------

    def test_01_sample_project_short_under_route_regression_fixed(self) -> None:
        """
        Critical regression test from Faz 1:
        'Mobil, backend ve web katmanlarındaki P0-01/P0-02 maddelerini plana göre
        tamamla; test, lint ve build kapılarını çalıştır.'
        Must classify as deep / high / workspace-write / gpt-5.6-sol.
        """
        prompt = (
            "Mobil, backend ve web katmanlarındaki P0-01/P0-02 maddelerini plana göre "
            "tamamla; test, lint ve build kapılarını çalıştır."
        )
        res = cx.classify(prompt, self.dummy_repo, self.policy)
        model = resolve_model(res, self.policy)

        self.assertEqual(res["tier"], "deep")
        self.assertEqual(res["reasoning"], "high")
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertTrue(res["mutating"])
        self.assertEqual(model, "gpt-5.6-sol")
        self.assertIn("task:multi-surface", res["risk_signals"]["task_shape"])
        self.assertIn("task:plan-reconcile", res["risk_signals"]["task_shape"])
        self.assertIn("task:verification-matrix", res["risk_signals"]["task_shape"])

    def test_02_sample_project_full_fixture_route(self) -> None:
        """
        Full 200+ line sample-project specification fixture.
        Must classify as deep / high / workspace-write / gpt-5.6-sol.
        """
        sample_lines = [
            "# SAMPLE-PROJECT MONOREPO ARCHITECTURE AUDIT AND REFACTOR PLAN",
            "Geliştirme planını incele ve P0-01 / P0-02 gap analizini gerçekleştir.",
            "",
            "## 1. Backend API Refactoring",
            "- Controllers must use strict DTOs and explicit view mapping.",
            "- Error codes must be structured machine-readable codes (AUTH_INVALID_TOKEN, etc).",
            "",
            "## 2. Dead Code Cleanup",
            "- Analyze mock graphs in mobile and backend packages.",
            "- Remove deprecated stub endpoints and mock models.",
            "",
            "## 3. Mandatory Quality Gates",
            "Run all verification suites across surfaces:",
            "Mobile: npx tsc --noEmit, npx jest --runInBand",
            "Backend: npm run lint, npx jest --runInBand, npm run build",
            "Web: npm run lint, npm run type-check, npm run build",
        ]
        for i in range(1, 200):
            sample_lines.append(f"Spec Item {i:03d}: Surface module {i} contract and test requirement (Türkçe: ğüşıöç).")
        full_text = "\n".join(sample_lines)

        res = cx.classify(full_text, self.dummy_repo, self.policy)
        model = resolve_model(res, self.policy)

        self.assertEqual(res["tier"], "deep")
        self.assertEqual(res["reasoning"], "high")
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertTrue(res["mutating"])
        self.assertEqual(model, "gpt-5.6-sol")

    def test_03_sample_project_short_and_full_invariant(self) -> None:
        """
        Invariant: Short and full sample-project tasks produce identical tier, reasoning, sandbox, and model.
        """
        short_prompt = (
            "Mobil, backend ve web katmanlarındaki P0-01/P0-02 maddelerini plana göre "
            "tamamla; test, lint ve build kapılarını çalıştır."
        )
        sample_lines = [
            "# SAMPLE-PROJECT MONOREPO PLAN",
            "Mobil, backend ve web katmanlarındaki P0-01/P0-02 maddelerini plana göre tamamla;",
            "test, lint ve build kapılarını çalıştır.",
        ] + [f"Line {i}: Detail" for i in range(100)]
        full_prompt = "\n".join(sample_lines)

        res_short = cx.classify(short_prompt, self.dummy_repo, self.policy)
        res_full = cx.classify(full_prompt, self.dummy_repo, self.policy)

        self.assertEqual(res_short["tier"], res_full["tier"])
        self.assertEqual(res_short["reasoning"], res_full["reasoning"])
        self.assertEqual(res_short["sandbox"], res_full["sandbox"])
        self.assertEqual(resolve_model(res_short, self.policy), resolve_model(res_full, self.policy))

    # -------------------------------------------------------------
    # 2. Turkish & English Parity (Section 17)
    # -------------------------------------------------------------

    def test_04_turkish_english_parity(self) -> None:
        """Turkish and English variants of complex multi-surface plan tasks both route to deep/high/write/Sol."""
        prompt_tr = (
            "Mobil, backend ve web katmanlarındaki P0-01/P0-02 maddelerini plana göre "
            "tamamla; test, lint, type-check ve build kapılarını çalıştır."
        )
        prompt_en = (
            "Complete the P0-01/P0-02 items across mobile, backend and web according to "
            "the implementation plan, then run the test, lint, type-check and build gates."
        )

        res_tr = cx.classify(prompt_tr, self.dummy_repo, self.policy)
        res_en = cx.classify(prompt_en, self.dummy_repo, self.policy)

        self.assertEqual(res_tr["tier"], "deep")
        self.assertEqual(res_en["tier"], "deep")
        self.assertEqual(res_tr["sandbox"], "workspace-write")
        self.assertEqual(res_en["sandbox"], "workspace-write")
        self.assertEqual(resolve_model(res_tr, self.policy), "gpt-5.6-sol")
        self.assertEqual(resolve_model(res_en, self.policy), "gpt-5.6-sol")

    # -------------------------------------------------------------
    # 3. Mutation Verbs & Negation Safety (Sections 6, 10, 11)
    # -------------------------------------------------------------

    def test_05_turkish_mutation_verbs(self) -> None:
        """Newly added and hardened Turkish mutation verbs trigger workspace-write."""
        verbs = [
            "P0 görevlerini tamamla",
            "Yeni auth kuralını uygula",
            "Dead code'ları temizle",
            "README metnini yeniden yaz",
            "Servis bağımlılıklarını güncelle",
            "Eski migration'ı sil",
        ]
        for v in verbs:
            res = cx.classify(v, {}, self.policy)
            self.assertTrue(res["mutating"], f"Failed for verb phrase: {v}")
            self.assertEqual(res["sandbox"], "workspace-write", f"Failed for verb phrase: {v}")

    def test_06_english_mutation_verbs(self) -> None:
        """English mutation verbs trigger workspace-write."""
        verbs = [
            "Complete the remaining items in checklist",
            "Clean up unused mock files",
            "Rewrite the parser module",
            "Implement new endpoint",
        ]
        for v in verbs:
            res = cx.classify(v, {}, self.policy)
            self.assertTrue(res["mutating"], f"Failed for verb phrase: {v}")
            self.assertEqual(res["sandbox"], "workspace-write", f"Failed for verb phrase: {v}")

    def test_07_negated_mutation_turkish(self) -> None:
        """Explicit Turkish negative write instructions yield read-only."""
        negations = [
            "Tüm projeyi güvenlik açıkları için baştan sona incele, değiştirme yapma.",
            "Bu migration'ın ne yaptığını açıkla, hiçbir şeyi değiştirme.",
            "Temizleme planını açıkla ama hiçbir dosyayı temizleme.",
            "Bu modülü nasıl refactor edeceğimizi anlat, kodu değiştirme.",
            "Backend ve web mimarisini açıkla. Hiçbir şeyi değiştirme.",
            "Tüm test ve build komutlarını listele, hiçbir komutu çalıştırma.",
        ]
        for n in negations:
            res = cx.classify(n, {}, self.policy)
            self.assertFalse(res["mutating"], f"Failed negation: {n}")
            self.assertEqual(res["sandbox"], "read-only", f"Failed negation: {n}")

    def test_08_negated_mutation_english(self) -> None:
        """Explicit English negative write instructions yield read-only."""
        negations = [
            "Audit the entire repository for vulnerabilities, do not modify any files.",
            "Explain what this database migration does, don't change anything.",
            "Explain the cleanup plan without modifying files.",
            "List test and lint commands for all services, do not execute commands.",
        ]
        for n in negations:
            res = cx.classify(n, {}, self.policy)
            self.assertFalse(res["mutating"], f"Failed negation: {n}")
            self.assertEqual(res["sandbox"], "read-only", f"Failed negation: {n}")

    # -------------------------------------------------------------
    # 4. Multi-Surface & Plan Complexity (Section 7)
    # -------------------------------------------------------------

    def test_09_multi_surface_mutating_vs_read_only(self) -> None:
        """Multi-surface with mutation increases complexity; multi-surface read-only is NOT deep."""
        # Mutating multi-surface
        res_mut = cx.classify("Update auth token exchange across mobile, backend and web", self.dummy_repo, self.policy)
        self.assertIn("task:multi-surface", res_mut["risk_signals"]["task_shape"])
        self.assertEqual(res_mut["sandbox"], "workspace-write")

        # Read-only multi-surface
        res_ro = cx.classify("Explain architecture across mobile, backend and web. Do not modify files.", self.dummy_repo, self.policy)
        self.assertIn("task:multi-surface-scope", res_ro["risk_signals"]["task_shape"])
        self.assertEqual(res_ro["sandbox"], "read-only")
        self.assertNotEqual(res_ro["tier"], "deep")

    def test_10_plan_reconciliation_mutating_vs_read_only(self) -> None:
        """Plan reconciliation with mutation increases complexity; read-only plan review is NOT deep."""
        # Mutating plan
        res_mut = cx.classify("P0-01 maddelerini plana uygun olarak tamamla", self.dummy_repo, self.policy)
        self.assertIn("task:plan-reconcile", res_mut["risk_signals"]["task_shape"])
        self.assertEqual(res_mut["sandbox"], "workspace-write")

        # Read-only plan
        res_ro = cx.classify("P0-01 maddelerini ve planı incele, sadece rapor yaz, kodu değiştirme", self.dummy_repo, self.policy)
        self.assertEqual(res_ro["sandbox"], "read-only")
        self.assertNotEqual(res_ro["tier"], "deep")

    # -------------------------------------------------------------
    # 5. Verification Matrix & False Positive Quoted Commands (Section 7 & 18)
    # -------------------------------------------------------------

    def test_11_verification_matrix_imperative_execution(self) -> None:
        """Explicit verification matrix with execution intent adds complexity."""
        prompt = "Backend ve mobile servislerini güncelle; jest testlerini, eslint linter'ı ve build kapılarını çalıştır."
        res = cx.classify(prompt, self.dummy_repo, self.policy)
        self.assertIn("task:verification-matrix", res["risk_signals"]["task_shape"])
        self.assertEqual(res["tier"], "deep")

    def test_12_verification_matrix_documentation_only(self) -> None:
        """Verification commands in documentation context must NOT trigger verification matrix signal."""
        prompt = "Backend, mobile ve web için test, lint ve build komutlarını dokümante et; hiçbir komutu çalıştırma."
        res = cx.classify(prompt, self.dummy_repo, self.policy)
        self.assertNotIn("task:verification-matrix", res["risk_signals"]["task_shape"])
        self.assertEqual(res["sandbox"], "read-only")

    def test_13_quoted_commands_not_verification_matrix(self) -> None:
        """Quoted commands inside text must not trigger verification-matrix."""
        prompt = "README'den şu örnek komutları kaldır: npm run build, npm test, npm run lint"
        res = cx.classify(prompt, {}, self.policy)
        self.assertNotIn("task:verification-matrix", res["risk_signals"]["task_shape"])
        self.assertEqual(res["sandbox"], "workspace-write")

    # -------------------------------------------------------------
    # 6. Safety & Non-Overroute Controls (Sections 12, 13, 14)
    # -------------------------------------------------------------

    def test_14_repo_wide_prettier_is_not_deep(self) -> None:
        """'Repo genelinde prettier çalıştır.' is workspace-write but NOT deep."""
        prompt = "Repo genelinde prettier çalıştır."
        res = cx.classify(prompt, {}, self.policy)
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertNotEqual(res["tier"], "deep")

    def test_15_repo_wide_markdown_cleanup_is_not_deep(self) -> None:
        """'Projede tüm markdown dosyalarındaki trailing whitespace'ı temizle.' is NOT deep."""
        prompt = "Projede tüm markdown dosyalarındaki trailing whitespace'ı temizle."
        res = cx.classify(prompt, {}, self.policy)
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertNotEqual(res["tier"], "deep")

    def test_16_long_documentation_rewrite_is_not_deep(self) -> None:
        """20 KB README rewrite is workspace-write but NOT deep solely because of length."""
        long_readme = ("Bu bir dokümantasyon metnidir ve detaylı açıklamalar içerir.\n" * 400)
        prompt = f"Aşağıdaki README metnini daha anlaşılır biçimde yeniden yaz:\n{long_readme}"
        res = cx.classify(prompt, {}, self.policy)
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertNotEqual(res["tier"], "deep")

    def test_17_short_critical_concurrency_remains_deep(self) -> None:
        """Short high-risk concurrency/migration tasks remain deep/high/Sol."""
        prompt = "Production migration race condition'ını düzelt, rollback testini ekle."
        res = cx.classify(prompt, {}, self.policy)
        self.assertEqual(res["tier"], "deep")
        self.assertEqual(res["reasoning"], "high")
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertEqual(resolve_model(res, self.policy), "gpt-5.6-sol")

    def test_18_broad_audit_read_only_escalates_to_deep(self) -> None:
        """'Tüm projeyi güvenlik açıkları için baştan sona incele, değiştirme yapma.' is deep/high/read-only/Sol."""
        prompt = "Tüm projeyi güvenlik açıkları için baştan sona incele, değiştirme yapma."
        res = cx.classify(prompt, {}, self.policy)
        self.assertEqual(res["tier"], "deep")
        self.assertEqual(res["reasoning"], "high")
        self.assertEqual(res["sandbox"], "read-only")
        self.assertFalse(res["mutating"])
        self.assertEqual(resolve_model(res, self.policy), "gpt-5.6-sol")

    # -------------------------------------------------------------
    # 7. Model Mapping & Integration (Sections 19, 20, 21)
    # -------------------------------------------------------------

    def test_19_model_mapping_contract(self) -> None:
        """choose_model correctly maps tiers to policy models."""
        self.assertEqual(cx.choose_model("routine", VISIBLE_MODELS, self.policy), "gpt-5.6-luna")
        self.assertEqual(cx.choose_model("standard", VISIBLE_MODELS, self.policy), "gpt-5.6-terra")
        self.assertEqual(cx.choose_model("deep", VISIBLE_MODELS, self.policy), "gpt-5.6-sol")

    def test_20_route_file_canary_integration(self) -> None:
        """CLI --route-file with sample-project short and full fixtures produces deep / high / Sol."""
        # A. Short fixture
        short_file = self.temp_dir / "short_sample_project.md"
        short_text = (
            "Mobil, backend ve web katmanlarındaki P0-01/P0-02 maddelerini plana göre "
            "tamamla; test, lint ve build kapılarını çalıştır."
        )
        short_file.write_bytes(short_text.encode("utf-8"))

        parser = build_parser()
        args_short = parser.parse_args(["--route-file", str(short_file)])
        resolved_short = resolve_prompt_source(args_short, self.temp_dir)
        res_short = cx.classify(resolved_short.prompt, self.dummy_repo, self.policy)
        self.assertEqual(res_short["tier"], "deep")
        self.assertEqual(res_short["sandbox"], "workspace-write")

        # B. Full fixture
        full_file = self.temp_dir / "full_sample_project.md"
        full_lines = [
            "# SAMPLE-PROJECT ARCHITECTURE PLAN",
            "Mobil, backend ve web katmanlarındaki P0-01/P0-02 maddelerini plana göre tamamla;",
            "test, lint ve build kapılarını çalıştır.",
        ] + [f"Item {i}: Contract spec" for i in range(150)]
        full_file.write_bytes("\n".join(full_lines).encode("utf-8"))

        args_full = parser.parse_args(["--route-file", str(full_file)])
        resolved_full = resolve_prompt_source(args_full, self.temp_dir)
        res_full = cx.classify(resolved_full.prompt, self.dummy_repo, self.policy)
        self.assertEqual(res_full["tier"], "deep")
        self.assertEqual(res_full["sandbox"], "workspace-write")

    def test_21_exact_16_scenario_adversarial_matrix(self) -> None:
        """Exact 16-case adversarial matrix from Section 15."""
        matrix = [
            # 1. Typo
            ("README'deki typo'ları düzelt.", "routine", "low", "workspace-write", "gpt-5.6-luna", False),
            # 2. Security review
            ("Security modülünü incele ve raporla.", "standard", "medium", "read-only", "gpt-5.6-terra", False),
            # 3. Whole-project audit read-only
            ("Tüm projeyi güvenlik açıkları için baştan sona incele, değiştirme yapma.", "deep", "high", "read-only", "gpt-5.6-sol", True),
            # 4. Auth race condition
            ("Backend auth modülündeki race condition'ı düzelt ve testleri çalıştır.", "deep", "high", "workspace-write", "gpt-5.6-sol", False),
            # 5. Sample-project short
            ("Mobil, backend ve web katmanlarındaki P0-01/P0-02 maddelerini plana göre tamamla; test, lint ve build kapılarını çalıştır.", "deep", "high", "workspace-write", "gpt-5.6-sol", False),
            # 6. Multi-surface read-only explanation
            ("Backend ve web mimarisini açıkla. Hiçbir şeyi değiştirme.", "standard", "medium", "read-only", "gpt-5.6-terra", False),
            # 7. Production migration race condition
            ("Production migration race condition'ını düzelt, rollback testini ekle.", "deep", "high", "workspace-write", "gpt-5.6-sol", False),
            # 8. Migration explanation read-only
            ("Bu migration'ın ne yaptığını açıkla, hiçbir şeyi değiştirme.", None, None, "read-only", None, False),
            # 9. Prettier repo-wide
            ("Repo genelinde prettier çalıştır.", "standard", "medium", "workspace-write", "gpt-5.6-terra", False),
            # 10. Trailing whitespace repo-wide (NOT deep)
            ("Projede tüm markdown dosyalarındaki trailing whitespace'ı temizle.", None, None, "workspace-write", None, False),
            # 11. Refactor explanation read-only
            ("Bu modülü nasıl refactor edeceğimizi anlat, kodu değiştirme.", "standard", "medium", "read-only", "gpt-5.6-terra", False),
            # 12. Cleanup plan explanation read-only
            ("Temizleme planını açıkla ama hiçbir dosyayı temizleme.", "routine", "low", "read-only", "gpt-5.6-luna", False),
            # 13. Test/lint/build commands documentation read-only (NOT deep, read-only)
            ("Backend, mobile ve web için test, lint ve build komutlarını dokümante et; hiçbir komutu çalıştırma.", None, None, "read-only", None, False),
            # 14. Multi-surface update + full verification matrix
            ("Backend, mobile ve web katmanlarını güncelle; tüm test, lint, type-check ve build kapılarını çalıştır.", "deep", "high", "workspace-write", "gpt-5.6-sol", False),
            # 15. 20 KB README rewrite (NOT deep)
            ("Aşağıdaki 20 KB README metnini daha anlaşılır biçimde yeniden yaz:\n" + ("Bu doküman metnidir.\n" * 400), None, None, "workspace-write", None, False),
            # 16. Sample-project exact full prompt
            ("Mobil, backend ve web katmanlarındaki P0-01/P0-02 maddelerini plana göre tamamla; test, lint ve build kapılarını çalıştır.\n" + ("Spec detail\n" * 100), "deep", "high", "workspace-write", "gpt-5.6-sol", False),
        ]

        for prompt, exp_tier, exp_reas, exp_sb, exp_model, exp_audit in matrix:
            repo_ctx = self.dummy_repo if ("Mobil" in prompt or "P0-01" in prompt) else {}
            res = cx.classify(prompt, repo_ctx, self.policy)
            model = resolve_model(res, self.policy)

            if exp_sb is not None:
                self.assertEqual(res["sandbox"], exp_sb, f"Sandbox mismatch for: {prompt[:50]}")
            if exp_tier is not None:
                self.assertEqual(res["tier"], exp_tier, f"Tier mismatch for: {prompt[:50]}")
            else:
                self.assertNotEqual(res["tier"], "deep", f"Should NOT be deep: {prompt[:50]}")
            if exp_reas is not None:
                self.assertEqual(res["reasoning"], exp_reas, f"Reasoning mismatch for: {prompt[:50]}")
            if exp_model is not None:
                self.assertEqual(model, exp_model, f"Model mismatch for: {prompt[:50]}")


if __name__ == "__main__":
    unittest.main()
