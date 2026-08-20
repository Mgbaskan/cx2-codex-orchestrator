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

## Updating Managed Files

To perform a rollback-safe managed upgrade of CX2 runtime files without touching your user state (database, logs, or custom policy configuration):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

> [!NOTE]
> Active CX processes must be closed prior to upgrading so the existing virtual environment (`runtime\venv`) can be safely replaced. If an upgrade fails midway, the installer performs a transactional managed-artifact rollback to restore the previous runtime and virtual environment.
