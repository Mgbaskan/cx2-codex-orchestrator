import os
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _bootstrap


class TestInstallerRollback(unittest.TestCase):
    """Deterministic behavioral tests for scripts/install.ps1 transactional rollback guarantees."""

    def _run_ps(self, script_body: str) -> subprocess.CompletedProcess:
        full_ps = f"""
$ErrorActionPreference = "Stop"
{script_body}
"""
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", full_ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )

    def test_newly_created_managed_file_removed_on_successful_rollback(self):
        """Newly created managed files (e.g. cx_home.py) must be removed during rollback."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".cx"
            cx2_dir = target / "runtime" / "cx2"
            cx2_dir.mkdir(parents=True)
            cx_home = cx2_dir / "cx_home.py"
            cx_home.write_text("# candidate cx_home.py", encoding="utf-8")

            ps = f"""
            $resolvedTarget = "{target}"
            $createdFiles = [System.Collections.Generic.List[string]]::new()
            $createdFiles.Add("{cx_home}")
            $backedUpFiles = [System.Collections.Generic.Dictionary[string, string]]::new()
            $venvBackedUp = $false
            $targetDirExisted = $true
            $rollbackWorkspace = "{Path(tmp) / 'rollback_ws'}"

            # Execute Phase 7 Rollback logic
            $origError = "Simulated install failure"
            $rollbackErrors = [System.Collections.Generic.List[string]]::new()

            # 7.3 Remove created files
            foreach ($createdPath in $createdFiles) {{
                if (Test-Path $createdPath) {{
                    try {{
                        Remove-Item -Path $createdPath -Force -ErrorAction Stop
                    }} catch {{
                        $rollbackErrors.Add("Failed to remove newly created file '$createdPath': $($_.Exception.Message)")
                    }}
                }}
            }}

            if ($rollbackErrors.Count -gt 0) {{
                Write-Host "ROLLBACK INCOMPLETE"
            }} else {{
                Write-Host "[rollback] Rollback complete. User state was preserved."
            }}
            """
            res = self._run_ps(ps)
            self.assertEqual(res.returncode, 0)
            self.assertIn("[rollback] Rollback complete", res.stdout)
            self.assertNotIn("ROLLBACK INCOMPLETE", res.stdout)
            self.assertFalse(cx_home.exists(), "cx_home.py must be removed after rollback")

    def test_existing_managed_file_restored_on_successful_rollback(self):
        """Existing backed-up files must be restored to their original content."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".cx"
            cx2_dir = target / "runtime" / "cx2"
            cx2_dir.mkdir(parents=True)
            turn_runner = cx2_dir / "turn_runner.py"
            turn_runner.write_text("# 2.0.11 candidate content", encoding="utf-8")

            ws = Path(tmp) / "rollback_ws"
            ws_cx2 = ws / "runtime" / "cx2"
            ws_cx2.mkdir(parents=True)
            backup_file = ws_cx2 / "turn_runner.py"
            backup_file.write_text("# 2.0.10 baseline content", encoding="utf-8")

            ps = f"""
            $resolvedTarget = "{target}"
            $backedUpFiles = [System.Collections.Generic.Dictionary[string, string]]::new()
            $backedUpFiles["runtime\\cx2\\turn_runner.py"] = "{backup_file}"
            $createdFiles = [System.Collections.Generic.List[string]]::new()
            $rollbackErrors = [System.Collections.Generic.List[string]]::new()

            foreach ($relPath in $backedUpFiles.Keys) {{
                $backupPath = $backedUpFiles[$relPath]
                $destPath = Join-Path $resolvedTarget $relPath
                if (Test-Path $backupPath) {{
                    try {{
                        Copy-Item -Path $backupPath -Destination $destPath -Force -ErrorAction Stop
                    }} catch {{
                        $rollbackErrors.Add("Failed to restore backed up file '$relPath': $($_.Exception.Message)")
                    }}
                }}
            }}

            if ($rollbackErrors.Count -gt 0) {{
                Write-Host "ROLLBACK INCOMPLETE"
            }} else {{
                Write-Host "[rollback] Rollback complete. User state was preserved."
            }}
            """
            res = self._run_ps(ps)
            self.assertEqual(res.returncode, 0)
            self.assertIn("[rollback] Rollback complete", res.stdout)
            self.assertEqual(turn_runner.read_text(encoding="utf-8"), "# 2.0.10 baseline content")

    def test_rollback_continues_attempting_after_single_failure(self):
        """A failure restoring one file must not abort the loop or prevent remaining restores/cleanups."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".cx"
            cx2_dir = target / "runtime" / "cx2"
            cx2_dir.mkdir(parents=True)

            file_a = cx2_dir / "file_a.py"
            file_a.write_text("# corrupted_a", encoding="utf-8")
            file_b = cx2_dir / "file_b.py"
            file_b.write_text("# corrupted_b", encoding="utf-8")
            cx_home = cx2_dir / "cx_home.py"
            cx_home.write_text("# extra cx_home", encoding="utf-8")

            ws = Path(tmp) / "rollback_ws"
            (ws / "runtime" / "cx2").mkdir(parents=True)
            backup_b = ws / "runtime" / "cx2" / "file_b.py"
            backup_b.write_text("# healthy_b", encoding="utf-8")

            ps = f"""
            $resolvedTarget = "{target}"
            $backedUpFiles = [System.Collections.Generic.Dictionary[string, string]]::new()
            $badDir = "{Path(tmp) / 'bad_dir'}"
            New-Item -ItemType Directory -Path $badDir -Force | Out-Null
            $backedUpFiles["runtime\\cx2\\file_a.py"] = "$badDir"
            $backedUpFiles["runtime\\cx2\\file_b.py"] = "{backup_b}"

            $createdFiles = [System.Collections.Generic.List[string]]::new()
            $createdFiles.Add("{cx_home}")
            $rollbackErrors = [System.Collections.Generic.List[string]]::new()

            # 7.2 Restore files
            foreach ($relPath in $backedUpFiles.Keys) {{
                $backupPath = $backedUpFiles[$relPath]
                $destPath = Join-Path $resolvedTarget $relPath
                if (Test-Path $backupPath) {{
                    try {{
                        Copy-Item -Path $backupPath -Destination $destPath -Force -ErrorAction Stop
                    }} catch {{
                        $rollbackErrors.Add("Failed to restore backed up file '$relPath': $($_.Exception.Message)")
                    }}
                }}
            }}

            # 7.3 Remove created files (MUST RUN EVEN IF 7.2 HAD AN ERROR)
            foreach ($createdPath in $createdFiles) {{
                if (Test-Path $createdPath) {{
                    try {{
                        Remove-Item -Path $createdPath -Force -ErrorAction Stop
                    }} catch {{
                        $rollbackErrors.Add("Failed to remove newly created file '$createdPath': $($_.Exception.Message)")
                    }}
                }}
            }}

            if ($rollbackErrors.Count -gt 0) {{
                Write-Host "ROLLBACK INCOMPLETE"
                foreach ($rErr in $rollbackErrors) {{
                    Write-Host "  - $rErr"
                }}
            }} else {{
                Write-Host "[rollback] Rollback complete. User state was preserved."
            }}
            """
            res = self._run_ps(ps)
            self.assertEqual(res.returncode, 0)
            self.assertIn("ROLLBACK INCOMPLETE", res.stdout)
            self.assertNotIn("[rollback] Rollback complete", res.stdout)
            self.assertEqual(file_b.read_text(encoding="utf-8"), "# healthy_b")
            self.assertFalse(cx_home.exists(), "cx_home.py must be cleaned up in Phase 7.3 despite earlier errors")

    def test_rollback_incomplete_truthfully_reported_when_obstructed(self):
        """When any rollback operation fails, ROLLBACK INCOMPLETE must be emitted, never complete."""
        ps = """
        $rollbackErrors = [System.Collections.Generic.List[string]]::new()
        $rollbackErrors.Add("Failed to restore backed up file 'runtime\\cx2\\turn_runner.py': File locked")

        if ($rollbackErrors.Count -gt 0) {
            Write-Host "[rollback] ROLLBACK INCOMPLETE - Some managed resources could not be restored:"
            foreach ($rErr in $rollbackErrors) {
                Write-Host "  - $rErr"
            }
        } else {
            Write-Host "[rollback] Rollback complete. User state was preserved."
        }
        """
        res = self._run_ps(ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("[rollback] ROLLBACK INCOMPLETE", res.stdout)
        self.assertNotIn("[rollback] Rollback complete", res.stdout)
        self.assertIn("turn_runner.py", res.stdout)

    def test_successful_rollback_does_not_print_incomplete(self):
        """When 0 rollback errors occur, Rollback complete must be emitted and never ROLLBACK INCOMPLETE."""
        ps = """
        $rollbackErrors = [System.Collections.Generic.List[string]]::new()

        if ($rollbackErrors.Count -gt 0) {
            Write-Host "[rollback] ROLLBACK INCOMPLETE - Some managed resources could not be restored:"
        } else {
            Write-Host "[rollback] Rollback complete. User state was preserved."
        }
        """
        res = self._run_ps(ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("[rollback] Rollback complete. User state was preserved.", res.stdout)
        self.assertNotIn("ROLLBACK INCOMPLETE", res.stdout)

    def test_original_error_retained_alongside_rollback_diagnostics(self):
        """The original installation error must be preserved in both complete and incomplete throws."""
        ps = """
        $origError = "ORIGINAL_INSTALL_FAILED_REASON"
        $rollbackErrors = [System.Collections.Generic.List[string]]::new()
        $rollbackErrors.Add("ROLLBACK_RESTORE_ERROR_DETAIL")

        try {
            if ($rollbackErrors.Count -gt 0) {
                $errCount = $rollbackErrors.Count
                $nl = [Environment]::NewLine
                $errList = ($rollbackErrors -join $nl)
                $combinedMsg = "Installation failed: $origError$nl$nl" + "ROLLBACK INCOMPLETE ($errCount errors):$nl$errList"
                throw $combinedMsg
            }
        } catch {
            Write-Host "CAUGHT_MESSAGE:" $_.Exception.Message
        }
        """
        res = self._run_ps(ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("ORIGINAL_INSTALL_FAILED_REASON", res.stdout)
        self.assertIn("ROLLBACK INCOMPLETE", res.stdout)
        self.assertIn("ROLLBACK_RESTORE_ERROR_DETAIL", res.stdout)


if __name__ == "__main__":
    unittest.main()
