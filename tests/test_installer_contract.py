import sys
from pathlib import Path
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap


class TestInstallerContract(unittest.TestCase):
    """Regression tests verifying structural and behavioural contracts of scripts/install.ps1."""

    @classmethod
    def setUpClass(cls):
        cls.install_script = REPO_ROOT / "scripts" / "install.ps1"
        cls.script_content = cls.install_script.read_text(encoding="utf-8")

    def test_installer_script_exists(self):
        self.assertTrue(self.install_script.is_file(), "scripts/install.ps1 not found")

    def test_python_version_gate_enforced(self):
        # Must perform semantic Python >= 3.10 check
        self.assertIn("sys.version_info >= (3, 10)", self.script_content)
        self.assertIn("Python 3.10+", self.script_content)

    def test_preflight_validates_before_target_mutation(self):
        # Preflight must check requirements, source, runtime, launcher, and compiler
        self.assertIn("requirements.txt", self.script_content)
        self.assertIn("src\\cx.py", self.script_content)
        self.assertIn("runtime\\cx2", self.script_content)
        self.assertIn("cx-launcher.cs", self.script_content)
        self.assertIn("build-launcher.ps1", self.script_content)
        self.assertIn("policy.example.json", self.script_content)
        self.assertIn("csc.exe", self.script_content)

    def test_no_path_update_parameter_exists(self):
        self.assertIn("[switch]$NoPathUpdate", self.script_content)
        self.assertIn("if (-not $NoPathUpdate)", self.script_content)

    def test_user_state_preserved(self):
        # Must preserve policy.json if it exists
        self.assertIn("$policyExisted = Test-Path $targetPolicy", self.script_content)
        self.assertIn("Preserved existing policy.json", self.script_content)
        # Must not delete data, logs, or config directories
        for user_dir in ["data", "logs", "config"]:
            self.assertIn(user_dir, self.script_content)
            # Ensure no command deletes user directories
            self.assertNotIn(f"Remove-Item -Path $resolvedTarget\\{user_dir}", self.script_content)

    def test_safe_venv_upgrade_strategy(self):
        # Must backup previous venv via rename and restore on failure
        self.assertIn("venv.backup-", self.script_content)
        self.assertIn("Move-Item -Path $venvDir -Destination $venvBackupDir", self.script_content)
        self.assertIn("Move-Item -Path $venvBackupDir -Destination $venvDir", self.script_content)

    def test_managed_artifact_rollback(self):
        # Must record backed up files and rollback workspace
        self.assertIn("$rollbackWorkspace", self.script_content)
        self.assertIn("$backedUpFiles", self.script_content)
        self.assertIn("$createdFiles", self.script_content)

    def test_doctor_verification_before_commit(self):
        # Must run target cx.exe --doctor and fail if exit code != 0
        self.assertIn("& $targetExe --doctor", self.script_content)
        self.assertIn("Installation self-check failed", self.script_content)

    def test_path_update_is_idempotent(self):
        self.assertIn("-notcontains $binDir", self.script_content)

    def test_rollback_explicit_error_collection(self):
        # Must instantiate rollback error collection rather than relying on global error suppression
        self.assertIn("$rollbackErrors = [System.Collections.Generic.List[string]]::new()", self.script_content)
        self.assertIn("$rollbackErrors.Count -gt 0", self.script_content)

    def test_rollback_per_operation_error_handling(self):
        # Must use per-operation try/catch blocks with -ErrorAction Stop so failures do not abort outer rollback
        self.assertIn('$rollbackErrors.Add("Failed to restore backed up file', self.script_content)
        self.assertIn('$rollbackErrors.Add("Failed to remove newly created file', self.script_content)
        self.assertIn('$rollbackErrors.Add("Failed to restore virtual environment', self.script_content)

    def test_rollback_status_reporting_contract(self):
        # Must clearly distinguish between ROLLBACK INCOMPLETE and Rollback complete
        self.assertIn("ROLLBACK INCOMPLETE", self.script_content)
        self.assertIn("[rollback] Rollback complete. User state was preserved.", self.script_content)

    def test_rollback_preserves_original_diagnostic(self):
        # Original error must be preserved in both complete and incomplete rollback paths
        self.assertIn("throw $origError", self.script_content)
        self.assertIn("Installation failed: $origError", self.script_content)

    def test_created_files_tracks_all_managed_additions(self):
        # Must pre-register created files including src/cx.py, runtime files, policy, launcher, and cmd
        self.assertIn('$createdFiles.Add($srcDest)', self.script_content)
        self.assertIn('$createdFiles.Add($dest)', self.script_content)
        self.assertIn('$createdFiles.Add($targetPolicy)', self.script_content)
        self.assertIn('$createdFiles.Add($targetExe)', self.script_content)
        self.assertIn('$createdFiles.Add($targetCmd)', self.script_content)

    def test_rollback_untouched_venv_preservation(self):
        # Must not delete existing venv on failure if venv was not backed up
        self.assertIn("elseif (-not $targetDirExisted -and (Test-Path $venvDir))", self.script_content)


if __name__ == "__main__":
    unittest.main()
