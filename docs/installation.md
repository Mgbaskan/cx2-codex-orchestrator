# Installation Guide

## Prerequisites

- **Windows 10 or 11**
- **PowerShell 5.1 or newer**
- **Python 3.10+**
- **Git for Windows**
- **OpenAI Codex** authenticated environment

## Automated Installation

Run the PowerShell installation script:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

## Manual Verification

Run the diagnostics check to verify your setup:

```powershell
cx --doctor
```

## Updating Managed Files

To update CX2 files without touching your local database or configuration:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1 -Force
```
