from __future__ import annotations

from pathlib import Path
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

import cx as production_cx
from cx2_runtime import (
    BROAD_AUDIT_DEVELOPER_INSTRUCTIONS,
    developer_instructions_for_route,
    is_broad_project_audit,
)
from session_adapter import (
    lean_config_with_web,
    thread_resume_params,
    thread_start_params,
)
from turn_runner import build_turn_input


class TestBroadAuditGuidance(unittest.TestCase):

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
        self.dummy_repo = {
            "root": Path("C:/Projects/docker_projects/seitoon"),
            "git": True,
            "tracked_files": 250,
            "tracked_files_bucket": "medium",
            "dirty_files": 0,
            "monorepo": False,
        }

    # =========================================================================
    # 1. Scope Detection (Positive & Negative Controls)
    # =========================================================================

    def test_scope_detection_turkish_broad_audit(self):
        """A: Turkish whole-project broad audit -> guidance ACTIVE."""
        prompt = "Dostum bu projeyi komple baştan aşağı inceleyip analiz et ve bana bulduğun açıkları / eksikleri / hataları raporla."
        route = production_cx.classify(prompt, self.dummy_repo, self.policy)
        self.assertTrue(is_broad_project_audit(route))
        instructions = developer_instructions_for_route(route)
        self.assertIsNotNone(instructions)
        self.assertEqual(instructions, BROAD_AUDIT_DEVELOPER_INSTRUCTIONS)

    def test_scope_detection_english_broad_audit(self):
        """E: English broad architecture/security audit -> guidance ACTIVE."""
        prompt = "Audit the entire repository for vulnerabilities, architectural flaws, and performance bottlenecks."
        route = production_cx.classify(prompt, self.dummy_repo, self.policy)
        self.assertTrue(is_broad_project_audit(route))
        instructions = developer_instructions_for_route(route)
        self.assertIsNotNone(instructions)
        self.assertEqual(instructions, BROAD_AUDIT_DEVELOPER_INSTRUCTIONS)

    def test_scope_detection_security_module_review_negative(self):
        """B: Specific security module review -> guidance OFF."""
        prompt = "backend/internal/auth modülünün güvenlik açıklarını incele"
        route = production_cx.classify(prompt, self.dummy_repo, self.policy)
        self.assertFalse(is_broad_project_audit(route))
        self.assertIsNone(developer_instructions_for_route(route))

    def test_scope_detection_docs_typo_review_negative(self):
        """C: Docs-wide typo review -> guidance OFF."""
        prompt = "Tüm projedeki typo ve yazım hatalarını düzelt"
        route = production_cx.classify(prompt, self.dummy_repo, self.policy)
        self.assertFalse(is_broad_project_audit(route))
        self.assertIsNone(developer_instructions_for_route(route))

    def test_scope_detection_race_condition_deep_task_negative(self):
        """D: Race condition deep task -> guidance OFF."""
        prompt = "Review race condition in worker channel sync"
        route = production_cx.classify(prompt, self.dummy_repo, self.policy)
        # Tier is deep, but it's NOT a broad whole-project audit
        self.assertEqual(route.get("tier"), "deep")
        self.assertFalse(is_broad_project_audit(route))
        self.assertIsNone(developer_instructions_for_route(route))

    # =========================================================================
    # 2. Instruction Content Semantics
    # =========================================================================

    def test_instruction_content_semantics(self):
        """Guidance must contain all key semantic budgeting and strategy markers."""
        text = BROAD_AUDIT_DEVELOPER_INSTRUCTIONS.lower()
        # 1. Prioritize risk surfaces
        self.assertIn("prioritize", text)
        # 2. Avoid exhaustive traversal / sampling
        self.assertTrue("exhaustive" in text or "sequential" in text)
        self.assertTrue("sampling" in text or "targeted" in text)
        # 3. Verification early (test, lint, typecheck)
        self.assertTrue("verification" in text or "test" in text)
        # 4. Reserve time / conclude
        self.assertTrue("conclude" in text or "final" in text or "report" in text)
        # 5. Disclose limitations / unverified areas
        self.assertTrue("limitations" in text or "unverified" in text or "blocked" in text)

    # =========================================================================
    # 3. User Prompt & Payload Integrity
    # =========================================================================

    def test_user_prompt_integrity_zero_mutation(self):
        """User prompt must remain 100% exact and never concatenated with guidance."""
        raw_prompt = "Dostum bu projeyi komple baştan aşağı inceleyip analiz et ve bana bulduğun açıkları / eksikleri / hataları raporla."
        turn_input = build_turn_input(raw_prompt)
        self.assertEqual(len(turn_input), 1)
        self.assertEqual(turn_input[0]["type"], "text")
        self.assertEqual(turn_input[0]["text"], raw_prompt)
        self.assertNotIn("Whole-project audit mode", turn_input[0]["text"])

    def test_process_local_config_injection(self):
        """Guidance is attached cleanly to thread config without modifying global files."""
        instructions = "Test guidance"
        cfg = lean_config_with_web("disabled", developer_instructions=instructions)
        self.assertEqual(cfg.get("developer_instructions"), instructions)

        # Thread start params
        start_p = thread_start_params(
            root=Path("C:/test"),
            model="gpt-5.6-sol",
            permissions="read-only",
            developer_instructions=instructions,
        )
        self.assertEqual(start_p["config"]["developer_instructions"], instructions)

        # Thread resume params
        resume_p = thread_resume_params(
            thread_id="th_123",
            root=Path("C:/test"),
            model="gpt-5.6-sol",
            permissions="read-only",
            developer_instructions=instructions,
        )
        self.assertEqual(resume_p["config"]["developer_instructions"], instructions)


if __name__ == "__main__":
    unittest.main()
