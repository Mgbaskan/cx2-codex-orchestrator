from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

import test_env
from client import AppServerClient


def hash_dir(path: Path) -> dict[str, str]:
    """Compute sha256 of all files in a directory to detect any mutations."""
    hashes = {}
    for p in sorted(path.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(path)).replace("\\", "/")
            hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


class TestExecutionEnvironment(unittest.TestCase):

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="cx2-test-fixture-"))
        self.workspace = self.temp_dir / "workspace"
        self.workspace.mkdir(parents=True)
        (self.workspace / "sample.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (self.workspace / "README.md").write_text("# Sample Project\n", encoding="utf-8")

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_environment_profile_creation_and_variables(self):
        """Environment profile must contain all required stack cache and temp overrides."""
        profile = test_env.build_test_environment()
        try:
            env = profile.env_overrides
            # General
            self.assertIn("TEMP", env)
            self.assertIn("TMP", env)
            self.assertIn("CX_TEST_TEMP_ROOT", env)
            self.assertTrue(Path(env["TEMP"]).exists())
            self.assertTrue(Path(env["TMP"]).exists())

            # Python
            self.assertEqual(env.get("PYTHONDONTWRITEBYTECODE"), "1")
            self.assertIn("PYTHONPYCACHEPREFIX", env)
            self.assertTrue(Path(env["PYTHONPYCACHEPREFIX"]).exists())

            # Go
            self.assertIn("GOCACHE", env)
            self.assertIn("GOTMPDIR", env)
            self.assertEqual(env.get("GOTELEMETRY"), "off")
            self.assertTrue(Path(env["GOCACHE"]).exists())
            self.assertTrue(Path(env["GOTMPDIR"]).exists())

            # Node
            self.assertIn("npm_config_cache", env)
            self.assertTrue(Path(env["npm_config_cache"]).exists())
        finally:
            profile.cleanup()

    def test_app_server_client_wiring_provisions_isolated_environment(self):
        """AppServerClient.start() must automatically provision and pass isolated environment to subprocess."""
        # Create a dummy executable script acting as codex app-server
        dummy_exe = self.temp_dir / "dummy_codex.cmd"
        dummy_exe.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

        client = AppServerClient(dummy_exe)
        self.assertIsNone(client._owned_env_profile)
        self.assertIsNone(client.launched_env)

        client.start()
        try:
            self.assertIsNotNone(client._owned_env_profile)
            self.assertIsNotNone(client.launched_env)
            self.assertTrue(client._owned_env_profile.temp_root.exists())

            env = client.launched_env
            self.assertIn("CX_TEST_TEMP_ROOT", env)
            self.assertIn("TEMP", env)
            self.assertIn("TMP", env)
            self.assertIn("GOCACHE", env)
            self.assertIn("GOTMPDIR", env)
            self.assertEqual(env.get("GOTELEMETRY"), "off")
            self.assertEqual(env.get("PYTHONDONTWRITEBYTECODE"), "1")
            self.assertIn("npm_config_cache", env)

            # Ensure workspace path is not used as temp/cache path
            for key in ["TEMP", "TMP", "GOCACHE", "GOTMPDIR", "npm_config_cache", "PYTHONPYCACHEPREFIX"]:
                self.assertNotIn(str(self.workspace), env[key])

            temp_root = client._owned_env_profile.temp_root
        finally:
            client.close()

        # After close, owned temp_root must be cleaned up
        self.assertFalse(temp_root.exists(), "AppServerClient.close() must clean up its owned temp_root")
        self.assertIsNone(client._owned_env_profile)

    def test_context_manager_lifecycle(self):
        """TestExecutionEnvironment context manager creates and cleans up automatically."""
        temp_path = None
        with test_env.TestExecutionEnvironment() as env_profile:
            temp_path = env_profile.temp_root
            self.assertTrue(temp_path.exists())
            self.assertIn("TEMP", env_profile.env_overrides)
        self.assertFalse(temp_path.exists())

    def test_python_canary_zero_workspace_mutation(self):
        """Python Canary: Running python test runner must NOT create __pycache__ in read-only workspace."""
        test_file = self.workspace / "test_sample.py"
        test_file.write_text(
            "import unittest\nimport sample\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertEqual(sample.add(1, 2), 3)\n",
            encoding="utf-8",
        )
        initial_hashes = hash_dir(self.workspace)

        with test_env.TestExecutionEnvironment() as env_profile:
            merged_env = {**os.environ, **env_profile.env_overrides}
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", str(self.workspace)],
                cwd=str(self.workspace),
                env=merged_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"Test failed: {proc.stderr}")

        # Verify no __pycache__ or extra files created in workspace
        pycache_dirs = list(self.workspace.rglob("__pycache__"))
        self.assertEqual(pycache_dirs, [], "No __pycache__ directories should exist in workspace")
        current_hashes = hash_dir(self.workspace)
        self.assertEqual(initial_hashes, current_hashes, "Workspace must remain 100% byte-for-byte identical")

    def test_go_canary_environment_isolation(self):
        """Go Canary: Running go test with isolated environment uses external GOCACHE/GOTMPDIR without workspace mutation."""
        go_exe = shutil.which("go")
        if not go_exe:
            self.skipTest("Go is not installed on PATH")

        go_workspace = self.temp_dir / "go_mod_workspace"
        go_workspace.mkdir(parents=True)
        (go_workspace / "go.mod").write_text("module example.com/testmod\n\ngo 1.21\n", encoding="utf-8")
        (go_workspace / "calc.go").write_text("package calc\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n", encoding="utf-8")
        (go_workspace / "calc_test.go").write_text(
            "package calc\n\nimport \"testing\"\n\nfunc TestAdd(t *testing.T) {\n\tif Add(2, 3) != 5 {\n\t\tt.Fail()\n\t}\n}\n",
            encoding="utf-8",
        )

        initial_hashes = hash_dir(go_workspace)

        with test_env.TestExecutionEnvironment() as env_profile:
            merged_env = {**os.environ, **env_profile.env_overrides}
            proc = subprocess.run(
                [go_exe, "test", "./..."],
                cwd=str(go_workspace),
                env=merged_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"go test failed: {proc.stderr}\nstdout: {proc.stdout}")

            # Verify GOCACHE was populated in external temp
            gocache_dir = Path(env_profile.env_overrides["GOCACHE"])
            self.assertTrue(gocache_dir.exists())

        current_hashes = hash_dir(go_workspace)
        self.assertEqual(initial_hashes, current_hashes, "Go workspace must have ZERO mutations")

    def test_node_canary_nested_child_process_spawn(self):
        """Node Canary: Node parent spawns child worker process inheriting isolated TEMP/TMP and npm_config_cache."""
        node_exe = shutil.which("node")
        if not node_exe:
            self.skipTest("Node is not installed on PATH")

        node_workspace = self.temp_dir / "node_workspace"
        node_workspace.mkdir(parents=True)
        child_js = node_workspace / "child.js"
        child_js.write_text(
            "console.log(JSON.stringify({temp: process.env.TEMP, npm_cache: process.env.npm_config_cache, cx_root: process.env.CX_TEST_TEMP_ROOT}));\n",
            encoding="utf-8",
        )

        parent_js = node_workspace / "parent.js"
        parent_js.write_text(
            "const { spawnSync } = require('child_process');\n"
            "const res = spawnSync(process.execPath, ['child.js'], { encoding: 'utf-8' });\n"
            "process.stdout.write(res.stdout);\n",
            encoding="utf-8",
        )

        initial_hashes = hash_dir(node_workspace)

        with test_env.TestExecutionEnvironment() as env_profile:
            merged_env = {**os.environ, **env_profile.env_overrides}
            proc = subprocess.run(
                [node_exe, "parent.js"],
                cwd=str(node_workspace),
                env=merged_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"node parent failed: {proc.stderr}")
            payload = json.loads(proc.stdout.strip())
            self.assertEqual(payload["cx_root"], str(env_profile.temp_root))
            self.assertEqual(payload["temp"], env_profile.env_overrides["TEMP"])
            self.assertEqual(payload["npm_cache"], env_profile.env_overrides["npm_config_cache"])

        current_hashes = hash_dir(node_workspace)
        self.assertEqual(initial_hashes, current_hashes, "Node workspace must have ZERO mutations")

    def test_cleanup_safety_adversarial_controls(self):
        """Adversarial cleanup tests: Refuse dangerous, arbitrary, or home-overlapping paths."""
        # 1. Refuse outside system temp
        fake_outside = test_env.ExecutionEnvironmentProfile(
            temp_root=self.workspace,
            env_overrides={},
            created_dirs=[],
        )
        with self.assertRaises(test_env.ExecutionEnvironmentError):
            fake_outside.cleanup()
        self.assertTrue(self.workspace.exists())

        # 2. Refuse system temp root directly
        system_temp = Path(tempfile.gettempdir()).resolve()
        fake_sys_temp = test_env.ExecutionEnvironmentProfile(
            temp_root=system_temp,
            env_overrides={},
            created_dirs=[],
        )
        with self.assertRaises(test_env.ExecutionEnvironmentError):
            fake_sys_temp.cleanup()
        self.assertTrue(system_temp.exists())

        # 3. Refuse user home directory
        user_home = Path.home().resolve()
        fake_home = test_env.ExecutionEnvironmentProfile(
            temp_root=user_home,
            env_overrides={},
            created_dirs=[],
        )
        with self.assertRaises(test_env.ExecutionEnvironmentError):
            fake_home.cleanup()
        self.assertTrue(user_home.exists())

        # 4. Refuse temp directory with wrong prefix
        wrong_prefix_dir = system_temp / f"not-cx-prefix-{tempfile.gettempprefix()}"
        wrong_prefix_dir.mkdir(exist_ok=True)
        try:
            fake_wrong = test_env.ExecutionEnvironmentProfile(
                temp_root=wrong_prefix_dir,
                env_overrides={},
                created_dirs=[],
            )
            with self.assertRaises(test_env.ExecutionEnvironmentError):
                fake_wrong.cleanup()
            self.assertTrue(wrong_prefix_dir.exists())
        finally:
            shutil.rmtree(wrong_prefix_dir, ignore_errors=True)

        # 5. Safe no-op on non-existent directory
        non_existent_dir = system_temp / "cx2-test-nonexistent-12345"
        safe_non_existent = test_env.ExecutionEnvironmentProfile(
            temp_root=non_existent_dir,
            env_overrides={},
            created_dirs=[],
        )
        # Should not raise
        safe_non_existent.cleanup()


if __name__ == "__main__":
    unittest.main()
