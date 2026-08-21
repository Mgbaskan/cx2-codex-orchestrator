import os
import sys
import tempfile
import unittest
from pathlib import Path
import shutil
import hashlib
import subprocess

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap

import test_env


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

    def test_safe_cleanup_only_removes_own_temp_root(self):
        """Cleanup must refuse to delete system root, user home, or arbitrary paths."""
        profile = test_env.build_test_environment()
        temp_root = profile.temp_root
        self.assertTrue(temp_root.exists())

        profile.cleanup()
        self.assertFalse(temp_root.exists())

        # Test safety boundary: arbitrary directory outside %TEMP% or not matching prefix
        fake_profile = test_env.ExecutionEnvironmentProfile(
            temp_root=self.workspace,
            env_overrides={},
            created_dirs=[],
        )
        with self.assertRaises(test_env.ExecutionEnvironmentError):
            fake_profile.cleanup()
        self.assertTrue(self.workspace.exists(), "Workspace must NOT be deleted by unsafe cleanup")

    def test_context_manager_lifecycle(self):
        """TestExecutionEnvironment context manager creates and cleans up automatically."""
        temp_path = None
        with test_env.TestExecutionEnvironment() as env_profile:
            temp_path = env_profile.temp_root
            self.assertTrue(temp_path.exists())
            self.assertIn("TEMP", env_profile.env_overrides)
        self.assertFalse(temp_path.exists())

    def test_python_zero_workspace_mutation(self):
        """Running python with test environment must NOT create __pycache__ in workspace."""
        initial_hashes = hash_dir(self.workspace)

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

    def test_go_environment_isolation_properties(self):
        """Go environment variables redirect build cache, gotmp, and disable telemetry."""
        with test_env.TestExecutionEnvironment() as env_profile:
            env = env_profile.env_overrides
            gocache = Path(env["GOCACHE"])
            gotmp = Path(env["GOTMPDIR"])
            self.assertTrue(gocache.is_dir())
            self.assertTrue(gotmp.is_dir())
            self.assertEqual(env["GOTELEMETRY"], "off")

            # Verify that gocache and gotmp are within the disposable temp_root
            self.assertTrue(gocache.is_relative_to(env_profile.temp_root))
            self.assertTrue(gotmp.is_relative_to(env_profile.temp_root))

    def test_node_environment_isolation_properties(self):
        """Node/npm environment variables redirect npm cache and temp to external root."""
        with test_env.TestExecutionEnvironment() as env_profile:
            env = env_profile.env_overrides
            npm_cache = Path(env["npm_config_cache"])
            temp_dir = Path(env["TEMP"])
            self.assertTrue(npm_cache.is_dir())
            self.assertTrue(temp_dir.is_dir())
            self.assertTrue(npm_cache.is_relative_to(env_profile.temp_root))
            self.assertTrue(temp_dir.is_relative_to(env_profile.temp_root))


if __name__ == "__main__":
    unittest.main()
