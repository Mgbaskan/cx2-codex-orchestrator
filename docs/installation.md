# Installation Guide

## Prerequisites

- **Windows 10 or 11**
- **PowerShell 5.1 or newer**
- **Python 3.10+** (enforced by installer; Python 3.12 is validated/recommended)
- **Git for Windows**
- **OpenAI Codex** authenticated environment

## Automated Installation

Run the PowerShell installation script:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

For isolated or testing environments where User PATH mutation is not desired, use `-NoPathUpdate`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1 -TargetDir "C:\path\to\target" -NoPathUpdate
```

## Manual Verification

Run the diagnostics check to verify your setup:

```powershell
cx --doctor
```

The installer runs only the offline `cx --doctor-offline` managed-file hash
check. It does not contact authenticated account/model state. Run `cx --doctor`
explicitly afterward when online user/runtime diagnostics are desired.

## Updating Managed Files

To perform a rollback-safe managed upgrade of CX2 runtime files without touching your user state (database, logs, or custom policy configuration):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

> [!NOTE]
> Active CX processes must be closed prior to upgrading so the existing virtual environment (`runtime\venv`) can be safely replaced. If an upgrade fails midway, the installer performs a transactional managed-artifact rollback to restore the previous runtime and virtual environment.

The installer owns `src/cx.py`, `bin/cx.exe`, `bin/cx.cmd`, and the Python files
under `runtime/cx2`. A hashed `managed-files.json` records that set and obsolete
managed Python modules are reconciled on upgrade. `data`, `policy.json`, logs,
and files outside the managed surface are preserved. Installer-owned cleanup is
retried and any remaining backup/temp path is reported explicitly.
