[CmdletBinding()]
param(
    [string]$TargetDir = "$env:USERPROFILE\.cx",
    [switch]$NoPathUpdate,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

Write-Host "=== CX2 Installation ===" -ForegroundColor Cyan
Write-Host "Target directory: $TargetDir"

# ==============================================================================
# PHASE 1: PRECHECK (Validation before any target directory mutation)
# ==============================================================================
Write-Host "[preflight] Validating installation prerequisites..." -ForegroundColor Cyan

# 1.1 Target path syntactic validation
if ([string]::IsNullOrWhiteSpace($TargetDir)) {
    throw "Target directory cannot be empty or whitespace."
}

try {
    $resolvedTarget = [System.IO.Path]::GetFullPath($TargetDir)
} catch {
    throw "Target directory path is invalid: $TargetDir ($($_.Exception.Message))"
}

# 1.2 Python presence & version check (semantic major/minor check >= 3.10)
$pythonCmd = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $pythonCmd) {
    throw "Python 3.10+ is required but 'python' was not found in PATH. Please install Python 3.10 or newer (Python 3.12 recommended) and add it to PATH."
}

$versionRaw = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'); sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>&1
$detectedVersion = ($versionRaw | Out-String).Trim()

if ($LASTEXITCODE -ne 0) {
    throw "Python 3.10+ is required (detected version: '$detectedVersion'). Please install Python 3.10 or newer (Python 3.12 recommended)."
}

Write-Host "[preflight] Python version OK: $detectedVersion" -ForegroundColor Green

# 1.3 Python venv module check
$venvCheck = & python -c "import venv" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Python 'venv' module is required but failed to import. Please ensure your Python distribution includes the standard venv module."
}

# 1.4 Repository source file preflight
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$reqFile = Join-Path $repoRoot "requirements.txt"
$srcCx = Join-Path $repoRoot "src\cx.py"
$runtimeSrcDir = Join-Path $repoRoot "runtime\cx2"
$launcherCs = Join-Path $repoRoot "launcher\cx-launcher.cs"
$buildLauncherScript = Join-Path $PSScriptRoot "build-launcher.ps1"
$policyExample = Join-Path $repoRoot "config\policy.example.json"

if (-not (Test-Path $reqFile)) {
    throw "Required file not found: $reqFile"
}
if (-not (Test-Path $srcCx)) {
    throw "Required source file not found: $srcCx"
}
if (-not (Test-Path $runtimeSrcDir)) {
    throw "Required runtime directory not found: $runtimeSrcDir"
}
$runtimeFiles = Get-ChildItem -Path $runtimeSrcDir -Filter "*.py"
if (-not $runtimeFiles -or $runtimeFiles.Count -eq 0) {
    throw "No Python source files found in runtime directory: $runtimeSrcDir"
}
if (-not (Test-Path $launcherCs)) {
    throw "Launcher source not found: $launcherCs"
}
if (-not (Test-Path $buildLauncherScript)) {
    throw "Build launcher script not found: $buildLauncherScript"
}
if (-not (Test-Path $policyExample)) {
    throw "Example policy configuration not found: $policyExample"
}

# 1.5 C# compiler check (.NET framework csc.exe)
$frameworkDir = [System.Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()
$csc = Join-Path $frameworkDir "csc.exe"
if (-not (Test-Path $csc)) {
    throw "C# compiler (csc.exe) not found in .NET Framework directory: $frameworkDir"
}

Write-Host "[preflight] All prerequisites verified." -ForegroundColor Green

# ==============================================================================
# TRANSACTION STATE
# ==============================================================================
$targetDirExisted = Test-Path $resolvedTarget
$rollbackWorkspace = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "cx2-installer-rollback-$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())-$PID")
$backedUpFiles = [System.Collections.Generic.Dictionary[string, string]]::new()
$createdFiles = [System.Collections.Generic.List[string]]::new()
$venvBackedUp = $false
$venvBackupDir = $null
$policyExisted = $false
$targetPolicy = Join-Path $resolvedTarget "policy.json"
$venvDir = Join-Path $resolvedTarget "runtime\venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$targetExe = Join-Path $resolvedTarget "bin\cx.exe"
$targetCmd = Join-Path $resolvedTarget "bin\cx.cmd"

try {
    # ==============================================================================
    # PHASE 2: PREPARE (Directory structure)
    # ==============================================================================
    if ($targetDirExisted) {
        Write-Host "[info] Target directory exists: $resolvedTarget" -ForegroundColor Yellow
        Write-Host "[info] Upgrading managed runtime files (user data and policy preserved)..."
    }

    $subdirs = @("bin", "src", "runtime\cx2", "data", "logs", "config")
    foreach ($sub in $subdirs) {
        $dirPath = Join-Path $resolvedTarget $sub
        if (-not (Test-Path $dirPath)) {
            New-Item -ItemType Directory -Path $dirPath -Force | Out-Null
        }
    }

    # ==============================================================================
    # PHASE 3: BACKUP (Preserve previous managed files and venv)
    # ==============================================================================
    New-Item -ItemType Directory -Path $rollbackWorkspace -Force | Out-Null

    # 3.1 Backup managed source files
    $managedRelPaths = [System.Collections.Generic.List[string]]::new()
    $managedRelPaths.Add("src\cx.py")
    $managedRelPaths.Add("bin\cx.exe")
    $managedRelPaths.Add("bin\cx.cmd")
    foreach ($rf in $runtimeFiles) {
        $managedRelPaths.Add("runtime\cx2\" + $rf.Name)
    }

    foreach ($relPath in $managedRelPaths) {
        $targetFilePath = Join-Path $resolvedTarget $relPath
        if (Test-Path $targetFilePath) {
            $backupFilePath = Join-Path $rollbackWorkspace $relPath
            $backupDir = Split-Path $backupFilePath -Parent
            if (-not (Test-Path $backupDir)) {
                New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
            }
            Copy-Item -Path $targetFilePath -Destination $backupFilePath -Force
            $backedUpFiles[$relPath] = $backupFilePath
        }
    }

    # 3.2 Check existing policy
    $policyExisted = Test-Path $targetPolicy

    # 3.3 Backup existing venv via same-volume rename
    if (Test-Path $venvDir) {
        $venvBackupDir = Join-Path $resolvedTarget "runtime\venv.backup-$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())-$PID"
        Write-Host "[install] Backing up existing virtual environment..." -ForegroundColor Cyan
        try {
            Move-Item -Path $venvDir -Destination $venvBackupDir -Force -ErrorAction Stop
            $venvBackedUp = $true
        } catch {
            throw "Failed to rename existing virtual environment at '$venvDir'. Active CX or Python processes may be running. Please close all active CX instances and retry. ($($_.Exception.Message))"
        }
    }

    # ==============================================================================
    # PHASE 4: INSTALL (Managed components)
    # ==============================================================================
    # 4.1 Create new canonical venv
    Write-Host "[install] Creating Python virtual environment at canonical path..." -ForegroundColor Cyan
    & python -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        throw "Failed to create Python virtual environment at '$venvDir'."
    }

    # 4.2 Install dependencies
    Write-Host "[install] Upgrading pip in runtime venv..." -ForegroundColor Cyan
    & $venvPython -m pip install --quiet --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip in virtual environment: $venvPython"
    }

    Write-Host "[install] Installing pinned dependencies from requirements.txt..." -ForegroundColor Cyan
    & $venvPython -m pip install --quiet -r $reqFile
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install dependencies from requirements.txt into virtual environment."
    }

    # 4.3 Copy managed source files
    Copy-Item $srcCx (Join-Path $resolvedTarget "src\cx.py") -Force
    if (-not $backedUpFiles.ContainsKey("src\cx.py")) {
        $createdFiles.Add((Join-Path $resolvedTarget "src\cx.py"))
    }

    foreach ($rf in $runtimeFiles) {
        $dest = Join-Path $resolvedTarget ("runtime\cx2\" + $rf.Name)
        Copy-Item $rf.FullName $dest -Force
        $rel = "runtime\cx2\" + $rf.Name
        if (-not $backedUpFiles.ContainsKey($rel)) {
            $createdFiles.Add($dest)
        }
    }

    # 4.4 Policy handling
    if (-not $policyExisted) {
        Copy-Item $policyExample $targetPolicy -Force
        $createdFiles.Add($targetPolicy)
        Write-Host "[install] Created default policy.json" -ForegroundColor Green
    } else {
        Write-Host "[install] Preserved existing policy.json" -ForegroundColor Green
    }

    # 4.5 Build launcher
    Write-Host "[install] Compiling native launcher..." -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File $buildLauncherScript -OutputPath $targetExe
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $targetExe)) {
        throw "Failed to compile launcher executable at: $targetExe"
    }
    if (-not $backedUpFiles.ContainsKey("bin\cx.exe")) {
        $createdFiles.Add($targetExe)
    }

    # 4.6 Create batch wrapper fallback
    $cmdContent = "@echo off`r`n`"%~dp0..\runtime\venv\Scripts\python.exe`" `"%~dp0..\runtime\cx2\cx2_cli.py`" %*"
    Set-Content -Path $targetCmd -Value $cmdContent -Encoding ASCII
    if (-not $backedUpFiles.ContainsKey("bin\cx.cmd")) {
        $createdFiles.Add($targetCmd)
    }

    # ==============================================================================
    # PHASE 5: VERIFY (Doctor self-check)
    # ==============================================================================
    Write-Host "`n[install] Running self-check (doctor)..." -ForegroundColor Cyan
    & $targetExe --doctor
    if ($LASTEXITCODE -ne 0) {
        throw "Installation self-check failed: '$targetExe --doctor' returned exit code $LASTEXITCODE."
    }

    # ==============================================================================
    # PHASE 6: COMMIT (Clean backups & optionally update PATH)
    # ==============================================================================
    # 6.1 Clean up previous venv backup
    if ($venvBackedUp -and (Test-Path $venvBackupDir)) {
        Remove-Item -Path $venvBackupDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    # 6.2 Clean up rollback workspace
    if (Test-Path $rollbackWorkspace) {
        Remove-Item -Path $rollbackWorkspace -Recurse -Force -ErrorAction SilentlyContinue
    }

    # 6.3 Update User PATH if not suppressed
    if (-not $NoPathUpdate) {
        $binDir = Join-Path $resolvedTarget "bin"
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $pathEntries = if ($userPath) { $userPath -split ";" | Where-Object { $_.Trim() -ne "" } } else { @() }
        if ($pathEntries -notcontains $binDir) {
            Write-Host "[install] Adding $binDir to User PATH..." -ForegroundColor Cyan
            $newPath = ($pathEntries + $binDir) -join ";"
            [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
            if (($env:Path -split ";" | Where-Object { $_.Trim() -ne "" }) -notcontains $binDir) {
                $env:Path = "$binDir;$env:Path"
            }
        }
    } else {
        Write-Host "[install] PATH update skipped (-NoPathUpdate specified)." -ForegroundColor Yellow
    }

    Write-Host "`n=== CX2 Installation Complete ===" -ForegroundColor Green
    Write-Host "You can now run 'cx' in your terminal."

} catch {
    # ==============================================================================
    # PHASE 7: ROLLBACK (Executed on any failure after preflight)
    # ==============================================================================
    $origError = $_.Exception.Message
    Write-Host "`n[error] Installation failed: $origError" -ForegroundColor Red
    Write-Host "[rollback] Initiating rollback of managed changes..." -ForegroundColor Yellow

    # 7.1 Restore previous virtual environment
    if ($venvBackedUp -and (Test-Path $venvBackupDir)) {
        Write-Host "[rollback] Restoring previous virtual environment..." -ForegroundColor Yellow
        if (Test-Path $venvDir) {
            Remove-Item -Path $venvDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        Move-Item -Path $venvBackupDir -Destination $venvDir -Force -ErrorAction SilentlyContinue
    } elseif (Test-Path $venvDir) {
        # If fresh install created incomplete venv, remove it
        Remove-Item -Path $venvDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    # 7.2 Restore backed up managed files
    foreach ($relPath in $backedUpFiles.Keys) {
        $backupPath = $backedUpFiles[$relPath]
        $destPath = Join-Path $resolvedTarget $relPath
        if (Test-Path $backupPath) {
            Copy-Item -Path $backupPath -Destination $destPath -Force -ErrorAction SilentlyContinue
        }
    }

    # 7.3 Remove files created fresh during this failed installation
    foreach ($createdPath in $createdFiles) {
        if (Test-Path $createdPath) {
            Remove-Item -Path $createdPath -Force -ErrorAction SilentlyContinue
        }
    }

    # 7.4 Clean up rollback temp directory
    if (Test-Path $rollbackWorkspace) {
        Remove-Item -Path $rollbackWorkspace -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Host "[rollback] Rollback complete. User state was preserved." -ForegroundColor Yellow
    throw $origError
}
