import unittest
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

import cx
import router_adapter
import budget_adapter
import session_adapter
import telemetry_adapter
import cx2_runtime


class TestRouting(unittest.TestCase):

    def setUp(self):
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
        self.repo = {"git": True, "clean": True, "monorepo": False, "dirty_files": 0}
        self.monorepo = {"git": True, "clean": True, "monorepo": True, "dirty_files": 0}

    # =========================================================================
    # BASELINE TESTS
    # =========================================================================

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
        self.assertFalse(res["mutating"])

    # =========================================================================
    # TARGETED ROUTING MATRIX (10 CASES)
    # =========================================================================

    def test_matrix_case_1_auth_button_color(self):
        """1. Routine UI mutation must not be pushed to deep by 'authentication' keyword."""
        res = cx.classify(
            "Change the Authentication button color in src/components/Login.tsx",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "routine")
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertTrue(res["mutating"])
        self.assertLessEqual(res["score"], self.policy["thresholds"]["routine_max"])

    def test_matrix_case_2_auth_readme_typo(self):
        """2. Routine documentation mutation must not be pushed to deep by 'authentication' keyword."""
        res = cx.classify(
            "Fix authentication typo in README.md",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "routine")
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertTrue(res["mutating"])
        self.assertLessEqual(res["score"], self.policy["thresholds"]["routine_max"])

    def test_matrix_case_3_auth_flow_read_only(self):
        """3. Read-only explanation of auth flow should be standard / read-only."""
        res = cx.classify(
            "Explain the authentication flow and do not modify files",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "standard")
        self.assertEqual(res["sandbox"], "read-only")
        self.assertFalse(res["mutating"])
        self.assertEqual(res["reasoning"], "medium")
        self.assertGreater(res["score"], self.policy["thresholds"]["routine_max"])
        self.assertLess(res["score"], self.policy["thresholds"]["deep_min"])

    def test_matrix_case_4_refresh_token_race_condition(self):
        """4. Concurrency bug on auth token surface must classify as deep."""
        res = cx.classify(
            "Fix the refresh token race condition in src/auth/refresh.ts",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "deep")
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertEqual(res["reasoning"], "high")
        self.assertGreaterEqual(res["score"], self.policy["thresholds"]["deep_min"])

    def test_matrix_case_5_deadlock_transaction(self):
        """5. Deadlock in transaction handling must classify as deep."""
        res = cx.classify(
            "Resolve deadlock in transaction handling",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "deep")
        self.assertEqual(res["reasoning"], "high")
        self.assertGreaterEqual(res["score"], self.policy["thresholds"]["deep_min"])

    def test_matrix_case_6_production_db_migration(self):
        """6. Production DB migration and rollback mutation must classify as deep."""
        res = cx.classify(
            "Update production database migration and rollback behavior",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "deep")
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertEqual(res["reasoning"], "high")
        self.assertGreaterEqual(res["score"], self.policy["thresholds"]["deep_min"])

    def test_matrix_case_7_kubernetes_secrets(self):
        """7. Kubernetes production deployment and secret handling mutation."""
        res = cx.classify(
            "Change Kubernetes production deployment and secret handling",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "deep")
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertEqual(res["reasoning"], "high")
        self.assertGreaterEqual(res["score"], self.policy["thresholds"]["deep_min"])

    def test_matrix_case_8_package_json_scripts(self):
        """8. Simple dependency manifest explanation must remain routine/read-only."""
        res = cx.classify(
            "Explain package.json scripts",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "routine")
        self.assertEqual(res["sandbox"], "read-only")
        self.assertFalse(res["mutating"])
        self.assertEqual(res["reasoning"], "low")

    def test_matrix_case_9_monorepo_auth_refactor(self):
        """9. Monorepo-wide cross-service refactor must classify as deep."""
        res = cx.classify(
            "Refactor authentication across all services in the monorepo",
            self.monorepo,
            self.policy,
        )
        self.assertEqual(res["tier"], "deep")
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertEqual(res["reasoning"], "high")
        self.assertGreaterEqual(res["score"], self.policy["thresholds"]["deep_min"])

    def test_matrix_case_10_security_review_read_only(self):
        """10. Security review with explicit read-only instructions should be standard / read-only."""
        res = cx.classify(
            "Review security module, read-only, do not modify files",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "standard")
        self.assertEqual(res["sandbox"], "read-only")
        self.assertFalse(res["mutating"])
        self.assertEqual(res["reasoning"], "medium")
        self.assertGreater(res["score"], self.policy["thresholds"]["routine_max"])
        self.assertLess(res["score"], self.policy["thresholds"]["deep_min"])

    def test_matrix_case_11_db_migration_explain_read_only(self):
        """11. Database migration explanation without mutation must not be deep."""
        res = cx.classify(
            "Explain what this database migration does and do not modify files",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["sandbox"], "read-only")
        self.assertFalse(res["mutating"])
        self.assertNotEqual(res["tier"], "deep")
        self.assertLess(res["score"], self.policy["thresholds"]["deep_min"])

    def test_matrix_case_12_auth_module_security_review_read_only(self):
        """12. Security review of auth module in read-only mode should be standard / read-only."""
        res = cx.classify(
            "Review the authentication module for security issues, read-only",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "standard")
        self.assertEqual(res["sandbox"], "read-only")
        self.assertFalse(res["mutating"])
        self.assertEqual(res["reasoning"], "medium")

    # =========================================================================
    # CALIBRATION REGRESSION TESTS (A - G)
    # =========================================================================

    def test_production_db_migration_rollback_is_deep(self):
        """A. production + DB migration + rollback mutation => deep."""
        res = cx.classify(
            "Update production database migration and rollback behavior",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "deep")
        self.assertEqual(res["sandbox"], "workspace-write")

    def test_db_migration_rollback_is_deep(self):
        """A2. DB migration + rollback mutation => deep."""
        res = cx.classify(
            "Update database migration rollback behavior",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "deep")
        self.assertEqual(res["sandbox"], "workspace-write")

    def test_read_only_auth_flow_analysis_is_standard(self):
        """B. read-only authentication flow analysis => standard/read-only."""
        res = cx.classify(
            "Explain the authentication flow and do not modify files",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "standard")
        self.assertEqual(res["sandbox"], "read-only")

    def test_read_only_security_module_review_is_standard(self):
        """C. read-only security module review => standard/read-only."""
        res = cx.classify(
            "Review security module, read-only, do not modify files",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "standard")
        self.assertEqual(res["sandbox"], "read-only")

    def test_auth_ui_color_mutation_is_routine(self):
        """D. authentication UI color mutation => routine/workspace-write."""
        res = cx.classify(
            "Change the Authentication button color in src/components/Login.tsx",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "routine")
        self.assertEqual(res["sandbox"], "workspace-write")

    def test_auth_readme_typo_is_routine(self):
        """E. authentication README typo => routine/workspace-write."""
        res = cx.classify(
            "Fix authentication typo in README.md",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "routine")
        self.assertEqual(res["sandbox"], "workspace-write")

    def test_migration_explanation_is_not_deep(self):
        """F. migration explanation/read-only without production/high-risk mutation => NOT deep."""
        res1 = cx.classify("Explain what this migration does", self.repo, self.policy)
        self.assertNotEqual(res1["tier"], "deep")

        res2 = cx.classify("Fix typo in migration documentation", self.repo, self.policy)
        self.assertEqual(res2["tier"], "routine")

    def test_rename_auth_label_is_routine(self):
        """Routine auth label rename remains routine."""
        res = cx.classify("Rename auth label in LoginForm.vue", self.repo, self.policy)
        self.assertEqual(res["tier"], "routine")
        self.assertEqual(res["sandbox"], "workspace-write")

    # =========================================================================
    # INVARIANT & PROPERTY TESTS
    # =========================================================================

    def test_critical_concurrency_dominance_over_routine_keywords(self):
        """Concurrency / deadlock signals must dominate even if routine keywords are present."""
        res = cx.classify(
            "Fix race condition in button CSS animation",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "deep")
        self.assertGreaterEqual(res["score"], self.policy["thresholds"]["deep_min"])

    def test_deadlock_dominance_over_documentation_keywords(self):
        """Deadlock signal must dominate even if documentation/comment keywords are present."""
        res = cx.classify(
            "Resolve deadlock mentioned in docs comment",
            self.repo,
            self.policy,
        )
        self.assertEqual(res["tier"], "deep")
        self.assertGreaterEqual(res["score"], self.policy["thresholds"]["deep_min"])

    def test_dirty_repo_does_not_force_deep(self):
        """Large dirty working tree alone must NOT force a trivial task into deep."""
        dirty_repo = {"git": True, "dirty_files": 50, "monorepo": False}
        res = cx.classify(
            "Change the button color",
            dirty_repo,
            self.policy,
        )
        self.assertNotEqual(res["tier"], "deep")
        self.assertEqual(res["tier"], "routine")

    def test_large_repo_does_not_force_deep(self):
        """Large repository tracked file count alone must NOT force a trivial task into deep."""
        large_repo = {"git": True, "tracked_files": 15000, "tracked_files_bucket": "large", "monorepo": False}
        res = cx.classify(
            "Fix typo in label",
            large_repo,
            self.policy,
        )
        self.assertNotEqual(res["tier"], "deep")
        self.assertEqual(res["tier"], "routine")

    def test_explicit_read_only_negations(self):
        """Various positive and Turkish negated write instructions must always yield read-only."""
        negated_prompts = [
            "do not modify any files, check the build",
            "don't edit files, review the architecture",
            "sadece oku, servis durumunu kontrol et",
            "dosyalarda degisiklik yapma, kodlari incele",
            "read-only mode: inspect database schema",
        ]
        for p in negated_prompts:
            res = cx.classify(p, self.repo, self.policy)
            self.assertEqual(res["sandbox"], "read-only", f"Failed on prompt: {p}")
            self.assertFalse(res["mutating"], f"Failed on prompt: {p}")

    def test_turkish_normalization_and_routing(self):
        """Turkish character normalization and domain matching."""
        # Routine UI with Turkish characters
        res1 = cx.classify("Authentication buton rengini değiştir", self.repo, self.policy)
        self.assertEqual(res1["tier"], "routine")
        self.assertEqual(res1["sandbox"], "workspace-write")

        # Routine typo with Turkish characters
        res2 = cx.classify("README.md içindeki kimlik doğrulama yazım hatasını düzelt", self.repo, self.policy)
        self.assertEqual(res2["tier"], "routine")
        self.assertEqual(res2["sandbox"], "workspace-write")

        # Read-only auth flow with Turkish negation -> standard / read-only
        res3 = cx.classify("Kimlik doğrulama akışını açıkla ve dosyalarda değişiklik yapma", self.repo, self.policy)
        self.assertEqual(res3["sandbox"], "read-only")
        self.assertEqual(res3["tier"], "standard")
        self.assertFalse(res3["mutating"])

        # Deep race condition in Turkish
        res4 = cx.classify("src/auth/refresh.ts içindeki token yenileme yarış durumunu düzelt", self.repo, self.policy)
        self.assertEqual(res4["tier"], "deep")
        self.assertEqual(res4["sandbox"], "workspace-write")

        # Deep deadlock in Turkish
        res5 = cx.classify("İşlem yönetimindeki kilitlenmeyi çöz", self.repo, self.policy)
        self.assertEqual(res5["tier"], "deep")

        # Deep production DB migration in Turkish
        res6 = cx.classify("Canlı ortam veritabanı migrasyonu ve geri alma davranışını güncelle", self.repo, self.policy)
        self.assertEqual(res6["tier"], "deep")
        self.assertEqual(res6["sandbox"], "workspace-write")

    def test_backward_compatible_keys_and_diagnostic_fields(self):
        """Returned classification dictionary must contain all legacy keys plus diagnostics."""
        res = cx.classify("Fix the login button", self.repo, self.policy)
        # Legacy keys
        self.assertIn("score", res)
        self.assertIn("tier", res)
        self.assertIn("reasoning", res)
        self.assertIn("sandbox", res)
        self.assertIn("mutating", res)
        self.assertIn("reasons", res)
        # New diagnostic keys
        self.assertIn("risk_signals", res)
        self.assertIn("score_breakdown", res)
        self.assertIn("router_version", res)
        self.assertEqual(res["router_version"], "1.2.2")

    def test_router_version_consistency_across_adapters(self):
        """All adapter EXPECTED_ROUTER_VERSION guards must match cx.ROUTER_VERSION."""
        self.assertEqual(cx.ROUTER_VERSION, "1.2.2")
        self.assertEqual(router_adapter.EXPECTED_ROUTER_VERSION, "1.2.2")
        self.assertEqual(budget_adapter.EXPECTED_ROUTER_VERSION, "1.2.2")
        self.assertEqual(session_adapter.EXPECTED_ROUTER_VERSION, "1.2.2")
        self.assertEqual(telemetry_adapter.EXPECTED_ROUTER_VERSION, "1.2.2")
        self.assertEqual(cx2_runtime.EXPECTED_ROUTER_VERSION, "1.2.2")

    def test_graceful_missing_repo_metadata(self):
        """Empty or incomplete repo metadata must not crash the classifier."""
        res = cx.classify("fix a small bug", {}, self.policy)
        self.assertIn(res["tier"], {"routine", "standard", "deep"})
        self.assertIn(res["sandbox"], {"read-only", "workspace-write"})

    def test_extract_referenced_paths(self):
        """Explicit file path extraction helper."""
        paths = cx.extract_referenced_paths(
            "Check src/auth/refresh.ts and README.md then review Dockerfile and prisma/schema.prisma"
        )
        self.assertIn("src/auth/refresh.ts", paths)
        self.assertIn("README.md", paths)
        self.assertIn("Dockerfile", paths)
        self.assertIn("prisma/schema.prisma", paths)

    # =========================================================================
    # PHASE 1 — WHOLE-PROJECT AUDIT / BROAD INSPECTION REGRESSION TESTS
    # =========================================================================

    def test_real_user_prompt_whole_project_audit_is_deep(self):
        """Real prompt: 'Dostum bu projeyi komple baştan aşağı inceleyip analiz et ve bana bulduğun açıkları / eksikleri / hataları raporla.' => deep."""
        prompt = "Dostum bu projeyi komple baştan aşağı inceleyip analiz et ve bana bulduğun açıkları / eksikleri / hataları raporla."
        res = cx.classify(prompt, self.repo, self.policy)
        self.assertEqual(res["tier"], "deep")
        self.assertEqual(res["reasoning"], "high")
        self.assertEqual(res["sandbox"], "read-only")
        self.assertFalse(res["mutating"])
        self.assertGreaterEqual(res["score"], self.policy["thresholds"]["deep_min"])

    def test_turkish_broad_audit_variations(self):
        """At least 10 Turkish broad-audit variations must classify as deep / read-only."""
        prompts = [
            "Bu projeyi komple baştan aşağı inceleyip analiz et ve açıkları, eksikleri ve hataları raporla",
            "Projeyi baştan sona incele ve sorunları bul",
            "Tüm codebase'i audit et ve security açıklarını ara",
            "Bütün sistemi incele, güvenlik açıklarını ve hataları çıkar",
            "Projeyi uçtan uca analiz et, eksikleri ve riskleri raporla",
            "Tüm repository genelinde security ve reliability audit yap",
            "Projeyi komple tara ve olası açıkları listele",
            "Bütün repoyu baştan aşağı gözden geçir, zafiyetleri tespit et",
            "Tüm kodları uçtan uca denetle, güvenlik sorunlarını ve eksikleri bul",
            "Proje genelinde mimari ve güvenlik denetimi yapıp riskleri çıkar",
            "Tüm projeyi analiz et ve mimari kusurları raporla",
        ]
        for p in prompts:
            res = cx.classify(p, self.repo, self.policy)
            self.assertEqual(res["tier"], "deep", f"Failed on prompt: {p} (tier={res['tier']}, score={res['score']})")
            self.assertEqual(res["sandbox"], "read-only", f"Failed sandbox on prompt: {p}")
            self.assertFalse(res["mutating"], f"Failed mutating on prompt: {p}")

    def test_english_broad_audit_variations(self):
        """At least 10 English broad-audit variations must classify as deep / read-only."""
        prompts = [
            "Audit the entire repository for vulnerabilities and bugs",
            "Review the whole project for flaws, missing protections and configuration issues",
            "Inspect the entire codebase end to end and report problems",
            "Scan the whole project for architectural, security and runtime issues",
            "Perform a full codebase audit and report all security holes and defects",
            "Comprehensive security and reliability audit across all repository files",
            "Inspect the entire repository, find vulnerabilities, gaps, and bugs",
            "End to end code review across the whole codebase for risks and weaknesses",
            "Thoroughly analyze the entire system and identify potential flaws and errors",
            "Deep scan across the entire project for vulnerabilities and bottlenecks",
            "Audit all services and packages for security flaws and missing protections",
        ]
        for p in prompts:
            res = cx.classify(p, self.repo, self.policy)
            self.assertEqual(res["tier"], "deep", f"Failed on prompt: {p} (tier={res['tier']}, score={res['score']})")
            self.assertEqual(res["sandbox"], "read-only", f"Failed sandbox on prompt: {p}")
            self.assertFalse(res["mutating"], f"Failed mutating on prompt: {p}")

    def test_broad_audit_negative_controls(self):
        """At least 10 negative control cases must NOT be escalated to deep."""
        controls = [
            # (prompt, expected_tier, expected_sandbox, expected_mutating)
            ("Review security module, read-only, do not modify files", "standard", "read-only", False),
            ("Review authentication module for security issues, read-only", "standard", "read-only", False),
            ("Explain the authentication flow and do not modify files", "standard", "read-only", False),
            ("Change the Authentication button color", "routine", "workspace-write", True),
            ("Fix authentication typo in README.md", "routine", "workspace-write", True),
            ("Explain package.json scripts", "routine", "read-only", False),
            ("Explain what this database migration does and do not modify files", "not_deep", "read-only", False),
            ("Inspect user authorization logic in auth.py, no changes", "standard", "read-only", False),
            ("Check database connection pool configuration in db.ts", "not_deep", "read-only", False),
            ("Kimlik doğrulama akışını açıkla ve dosyalarda değişiklik yapma", "standard", "read-only", False),
            ("Güvenlik modülünü incele, salt okunur, hiçbir dosyayı değiştirme", "standard", "read-only", False),
        ]
        for p, exp_tier, exp_sandbox, exp_mutating in controls:
            res = cx.classify(p, self.repo, self.policy)
            if exp_tier == "not_deep":
                self.assertNotEqual(res["tier"], "deep", f"Negative control failed on prompt: {p} (tier={res['tier']}, score={res['score']})")
            else:
                self.assertEqual(res["tier"], exp_tier, f"Negative control failed on prompt: {p} (tier={res['tier']}, score={res['score']})")
            self.assertEqual(res["sandbox"], exp_sandbox, f"Negative control sandbox failed on prompt: {p}")
            self.assertEqual(res["mutating"], exp_mutating, f"Negative control mutating failed on prompt: {p}")

        # Trivial task on large and dirty repo must remain routine
        res_large = cx.classify("Fix typo in README.md", {"git": True, "clean": True, "monorepo": False, "dirty_files": 0, "tracked_files_bucket": "large"}, self.policy)
        self.assertEqual(res_large["tier"], "routine")

        res_dirty = cx.classify("Change button color to blue", {"git": True, "clean": False, "monorepo": False, "dirty_files": 100}, self.policy)
        self.assertEqual(res_dirty["tier"], "routine")

    def test_adversarial_negative_controls(self):
        """Adversarial negative controls: broad-scope keywords in routine/doc/typo/formatting tasks must NOT be deep."""
        cases = [
            ("Review all documentation in the repository for typos", "not_deep", "read-only", False),
            ("Fix typos across all README and docs files", "not_deep", "workspace-write", True),
            ("Check the entire repository for formatting issues in Markdown files", "not_deep", "read-only", False),
            ("Rename a label across all documentation files", "not_deep", "workspace-write", True),
            ("Review all package.json scripts and explain what they do", "not_deep", "read-only", False),
            ("Tüm projedeki README ve dokümantasyon yazım hatalarını kontrol et", "not_deep", "read-only", False),
            ("Bütün dokümantasyon dosyalarındaki yazım hatalarını düzelt", "not_deep", "workspace-write", True),
            ("Proje genelindeki Markdown biçimlendirmesini kontrol et", "not_deep", "read-only", False),
            ("Projede geçen şirket adını bütün dokümantasyon dosyalarında yeniden adlandır", "not_deep", "workspace-write", True),
            ("Tüm package.json scriptlerini incele ve ne yaptıklarını açıkla", "not_deep", "read-only", False),
        ]
        for p, exp_tier, exp_sandbox, exp_mutating in cases:
            res = cx.classify(p, self.repo, self.policy)
            if exp_tier == "not_deep":
                self.assertNotEqual(res["tier"], "deep", f"Adversarial negative control failed on prompt: {p} (tier={res['tier']}, score={res['score']})")
            else:
                self.assertEqual(res["tier"], exp_tier, f"Adversarial negative control failed on prompt: {p} (tier={res['tier']}, score={res['score']})")
            self.assertEqual(res["sandbox"], exp_sandbox, f"Adversarial negative sandbox failed on prompt: {p}")
            self.assertEqual(res["mutating"], exp_mutating, f"Adversarial negative mutating failed on prompt: {p}")

    def test_adversarial_positive_controls(self):
        """Adversarial positive controls: true whole-project audits MUST be deep / read-only."""
        cases = [
            "Dostum bu projeyi komple baştan aşağı inceleyip analiz et ve bana bulduğun açıkları / eksikleri / hataları raporla.",
            "Audit the entire repository for vulnerabilities and bugs",
            "Inspect the entire codebase end to end and report architectural, security and runtime issues",
            "Bütün sistemi incele, güvenlik açıklarını, mimari sorunları ve hataları raporla",
            "Scan the whole project for security vulnerabilities, configuration weaknesses and runtime failures",
        ]
        for p in cases:
            res = cx.classify(p, self.repo, self.policy)
            self.assertEqual(res["tier"], "deep", f"Adversarial positive control failed on prompt: {p} (tier={res['tier']}, score={res['score']})")
            self.assertEqual(res["reasoning"], "high", f"Adversarial positive reasoning failed on prompt: {p}")
            self.assertEqual(res["sandbox"], "read-only", f"Adversarial positive sandbox failed on prompt: {p}")
            self.assertFalse(res["mutating"], f"Adversarial positive mutating failed on prompt: {p}")


if __name__ == "__main__":
    unittest.main()
