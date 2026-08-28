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
$releaseVersionFile = Join-Path $runtimeSrcDir "release_version.py"
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
if (-not (Test-Path $releaseVersionFile)) {
    throw "Release version source not found: $releaseVersionFile"
}
$releaseVersionMatch = [regex]::Match(
    (Get-Content -LiteralPath $releaseVersionFile -Raw),
    'CX2_VERSION\s*=\s*"([^"]+)"'
)
if (-not $releaseVersionMatch.Success) {
    throw "CX2_VERSION could not be read from $releaseVersionFile"
}
$releaseVersion = $releaseVersionMatch.Groups[1].Value
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
$venvExistedBefore = Test-Path $venvDir
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$targetExe = Join-Path $resolvedTarget "bin\cx.exe"
$targetCmd = Join-Path $resolvedTarget "bin\cx.cmd"
$targetManifest = Join-Path $resolvedTarget "runtime\cx2\managed-files.json"
$cleanupResidues = [System.Collections.Generic.List[string]]::new()

function Remove-InstallerArtifact {
    param([Parameter(Mandatory=$true)][string]$LiteralPath)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        if (-not (Test-Path -LiteralPath $LiteralPath)) { return $true }
        try {
            Remove-Item -LiteralPath $LiteralPath -Recurse -Force -ErrorAction Stop
        } catch {
            if ($attempt -lt 3) { Start-Sleep -Milliseconds 150 }
        }
    }
    return -not (Test-Path -LiteralPath $LiteralPath)
}

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
    $managedRelPaths.Add("runtime\cx2\managed-files.json")
    foreach ($rf in $runtimeFiles) {
        $managedRelPaths.Add("runtime\cx2\" + $rf.Name)
    }

    # Before manifests existed, runtime\cx2\*.py was already an explicitly
    # installer-managed surface. Include old modules so removal is rollback-safe.
    $existingRuntimeDir = Join-Path $resolvedTarget "runtime\cx2"
    if (Test-Path $existingRuntimeDir) {
        foreach ($existingPy in Get-ChildItem -LiteralPath $existingRuntimeDir -Filter "*.py" -File) {
            $oldRel = "runtime\cx2\" + $existingPy.Name
            if (-not $managedRelPaths.Contains($oldRel)) { $managedRelPaths.Add($oldRel) }
        }
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
    if ($venvExistedBefore) {
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
    $srcDest = Join-Path $resolvedTarget "src\cx.py"
    if (-not $backedUpFiles.ContainsKey("src\cx.py") -and -not $createdFiles.Contains($srcDest)) {
        $createdFiles.Add($srcDest)
    }
    Copy-Item $srcCx $srcDest -Force

    foreach ($rf in $runtimeFiles) {
        $dest = Join-Path $resolvedTarget ("runtime\cx2\" + $rf.Name)
        $rel = "runtime\cx2\" + $rf.Name
        if (-not $backedUpFiles.ContainsKey($rel) -and -not $createdFiles.Contains($dest)) {
            $createdFiles.Add($dest)
        }
        Copy-Item $rf.FullName $dest -Force
    }

    # Remove only obsolete files from the explicitly managed Python runtime
    # surface. They were included in the rollback backup above.
    $expectedRuntimeNames = @($runtimeFiles | ForEach-Object { $_.Name })
    foreach ($installedPy in Get-ChildItem -LiteralPath (Join-Path $resolvedTarget "runtime\cx2") -Filter "*.py" -File) {
        if ($expectedRuntimeNames -notcontains $installedPy.Name) {
            Remove-Item -LiteralPath $installedPy.FullName -Force -ErrorAction Stop
            Write-Host "[install] Removed obsolete managed module: $($installedPy.Name)"
        }
    }

    # 4.4 Policy handling
    if (-not $policyExisted) {
        if (-not $createdFiles.Contains($targetPolicy)) {
            $createdFiles.Add($targetPolicy)
        }
        Copy-Item $policyExample $targetPolicy -Force
        Write-Host "[install] Created default policy.json" -ForegroundColor Green
    } else {
        Write-Host "[install] Preserved existing policy.json" -ForegroundColor Green
    }

    # 4.5 Build launcher
    if (-not $backedUpFiles.ContainsKey("bin\cx.exe") -and -not $createdFiles.Contains($targetExe)) {
        $createdFiles.Add($targetExe)
    }
    Write-Host "[install] Compiling native launcher..." -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File $buildLauncherScript -OutputPath $targetExe
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $targetExe)) {
        throw "Failed to compile launcher executable at: $targetExe"
    }

    # 4.6 Create batch wrapper fallback
    if (-not $backedUpFiles.ContainsKey("bin\cx.cmd") -and -not $createdFiles.Contains($targetCmd)) {
        $createdFiles.Add($targetCmd)
    }
    $cmdContent = "@echo off`r`n`"%~dp0..\runtime\venv\Scripts\python.exe`" `"%~dp0..\runtime\cx2\cx2_cli.py`" %*"
    Set-Content -Path $targetCmd -Value $cmdContent -Encoding ASCII

    # 4.7 Offline release provenance for the complete managed source surface.
    if (-not $backedUpFiles.ContainsKey("runtime\cx2\managed-files.json") -and -not $createdFiles.Contains($targetManifest)) {
        $createdFiles.Add($targetManifest)
    }
    $manifestRelPaths = [System.Collections.Generic.List[string]]::new()
    $manifestRelPaths.Add("src\cx.py")
    $manifestRelPaths.Add("bin\cx.exe")
    $manifestRelPaths.Add("bin\cx.cmd")
    foreach ($rf in $runtimeFiles) { $manifestRelPaths.Add("runtime\cx2\" + $rf.Name) }
    $managedHashes = [ordered]@{}
    foreach ($relPath in ($manifestRelPaths | Sort-Object)) {
        $managedHashes[$relPath] = (Get-FileHash -LiteralPath (Join-Path $resolvedTarget $relPath) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $manifest = [ordered]@{
        schema = 1
        version = $releaseVersion
        sha256 = $managedHashes
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText(
        $targetManifest,
        $manifestJson + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )

    # ==============================================================================
    # PHASE 5: VERIFY (Doctor self-check)
    # ==============================================================================
    Write-Host "`n[install] Running structural offline self-check..." -ForegroundColor Cyan
    & $targetExe --doctor-offline
    if ($LASTEXITCODE -ne 0) {
        throw "Installation structural self-check failed: '$targetExe --doctor-offline' returned exit code $LASTEXITCODE."
    }
    Write-Host "[install] Running online account/model diagnostics..." -ForegroundColor Cyan
    & $targetExe --doctor
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Online doctor reported account/model unavailability (exit $LASTEXITCODE); structurally verified installation is retained."
    }

    # ==============================================================================
    # PHASE 6: COMMIT (Clean backups & optionally update PATH)
    # ==============================================================================
    # 6.1 Clean up previous venv backup
    if ($venvBackedUp -and (Test-Path $venvBackupDir)) {
        if (-not (Remove-InstallerArtifact -LiteralPath $venvBackupDir)) {
            $cleanupResidues.Add($venvBackupDir)
        }
    }

    # 6.2 Clean up rollback workspace
    if (Test-Path $rollbackWorkspace) {
        if (-not (Remove-InstallerArtifact -LiteralPath $rollbackWorkspace)) {
            $cleanupResidues.Add($rollbackWorkspace)
        }
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

    if ($cleanupResidues.Count -gt 0) {
        Write-Warning "Installation succeeded with installer-owned cleanup residue: $($cleanupResidues -join ', ')"
        Write-Host "`n=== CX2 Installation Complete (cleanup residue reported) ===" -ForegroundColor Yellow
    } else {
        Write-Host "`n=== CX2 Installation Complete ===" -ForegroundColor Green
    }
    Write-Host "You can now run 'cx' in your terminal."

} catch {
    # ==============================================================================
    # PHASE 7: ROLLBACK (Executed on any failure after preflight)
    # ==============================================================================
    $origError = $_.Exception.Message
    Write-Host "`n[error] Installation failed: $origError" -ForegroundColor Red
    Write-Host "[rollback] Initiating rollback of managed changes..." -ForegroundColor Yellow

    $rollbackErrors = [System.Collections.Generic.List[string]]::new()

    # 7.1 Restore previous virtual environment
    if ($venvBackedUp -and (Test-Path $venvBackupDir)) {
        Write-Host "[rollback] Restoring previous virtual environment..." -ForegroundColor Yellow
        try {
            if (Test-Path $venvDir) {
                Remove-Item -Path $venvDir -Recurse -Force -ErrorAction Stop
            }
            Move-Item -Path $venvBackupDir -Destination $venvDir -Force -ErrorAction Stop
        } catch {
            $rollbackErrors.Add("Failed to restore virtual environment from '$venvBackupDir' to '$venvDir': $($_.Exception.Message)")
        }
    } elseif (-not $venvExistedBefore -and (Test-Path $venvDir)) {
        # If this installation created a new venv where none existed before, remove it
        try {
            Remove-Item -Path $venvDir -Recurse -Force -ErrorAction Stop
        } catch {
            $rollbackErrors.Add("Failed to remove newly created virtual environment at '$venvDir': $($_.Exception.Message)")
        }
    }

    # 7.2 Restore backed up managed files
    foreach ($relPath in $backedUpFiles.Keys) {
        $backupPath = $backedUpFiles[$relPath]
        $destPath = Join-Path $resolvedTarget $relPath
        if (Test-Path $backupPath) {
            try {
                Copy-Item -Path $backupPath -Destination $destPath -Force -ErrorAction Stop
            } catch {
                $rollbackErrors.Add("Failed to restore backed up file '$relPath': $($_.Exception.Message)")
            }
        }
    }

    # 7.3 Remove files created fresh during this failed installation
    foreach ($createdPath in $createdFiles) {
        if (Test-Path $createdPath) {
            try {
                Remove-Item -Path $createdPath -Force -ErrorAction Stop
            } catch {
                $rollbackErrors.Add("Failed to remove newly created file '$createdPath': $($_.Exception.Message)")
            }
        }
    }

    # 7.4 Clean up rollback temp directory
    if (Test-Path $rollbackWorkspace) {
        try {
            Remove-Item -Path $rollbackWorkspace -Recurse -Force -ErrorAction Stop
        } catch {
            Write-Host "[rollback] Warning: Failed to clean up rollback workspace at '$rollbackWorkspace': $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    # 7.5 Status reporting and diagnostic preservation
    if ($rollbackErrors.Count -gt 0) {
        Write-Host ""
        Write-Host "[rollback] ROLLBACK INCOMPLETE - Some managed resources could not be restored:" -ForegroundColor Red
        foreach ($rErr in $rollbackErrors) {
            Write-Host "  - $rErr" -ForegroundColor Red
        }
        $errCount = $rollbackErrors.Count
        $nl = [Environment]::NewLine
        $errList = ($rollbackErrors -join $nl)
        $combinedMsg = "Installation failed: $origError$nl$nl" + "ROLLBACK INCOMPLETE ($errCount errors):$nl$errList"
        throw $combinedMsg
    } else {
        Write-Host "[rollback] Rollback complete. User state was preserved." -ForegroundColor Yellow
        throw $origError
    }
}
