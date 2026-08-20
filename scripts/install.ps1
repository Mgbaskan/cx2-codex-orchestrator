[CmdletBinding()]
param(
    [string]$TargetDir = "$env:USERPROFILE\.cx",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

Write-Host "=== CX2 Installation ===" -ForegroundColor Cyan
Write-Host "Target directory: $TargetDir"

if ((Test-Path $TargetDir) -and (-not $Force)) {
    Write-Host "[info] Target directory already exists: $TargetDir" -ForegroundColor Yellow
    Write-Host "Updating managed files..."
}

# 1. Create directory structure
$subdirs = @("bin", "src", "runtime\cx2", "data", "logs", "config")
foreach ($sub in $subdirs) {
    $dirPath = Join-Path $TargetDir $sub
    if (-not (Test-Path $dirPath)) {
        New-Item -ItemType Directory -Path $dirPath -Force | Out-Null
    }
}

# 2. Detect Python
$pythonCmd = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $pythonCmd) {
    throw "Python 3.10+ is required but was not found in PATH."
}

# 3. Create virtual environment
$venvDir = Join-Path $TargetDir "runtime\venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[install] Creating Python virtual environment..." -ForegroundColor Cyan
    & python -m venv $venvDir
}

# 4. Install dependencies
$reqFile = Join-Path $PSScriptRoot "..\requirements.txt"
if (Test-Path $reqFile) {
    Write-Host "[install] Installing dependencies from requirements.txt..." -ForegroundColor Cyan
    & $venvPython -m pip install --quiet --upgrade pip
    & $venvPython -m pip install --quiet -r $reqFile
}

# 5. Copy source files
$repoRoot = Join-Path $PSScriptRoot ".."
Copy-Item (Join-Path $repoRoot "src\cx.py") (Join-Path $TargetDir "src\cx.py") -Force

$runtimeSrc = Join-Path $repoRoot "runtime\cx2"
Get-ChildItem -Path $runtimeSrc -Filter "*.py" | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $TargetDir "runtime\cx2") -Force
}

# 6. Copy default policy if not exists
$targetPolicy = Join-Path $TargetDir "policy.json"
$examplePolicy = Join-Path $repoRoot "config\policy.example.json"
if (-not (Test-Path $targetPolicy)) {
    Copy-Item $examplePolicy $targetPolicy -Force
    Write-Host "[install] Created default policy.json" -ForegroundColor Green
}

# 7. Build and install launcher
$targetExe = Join-Path $TargetDir "bin\cx.exe"
$buildScript = Join-Path $PSScriptRoot "build-launcher.ps1"
& powershell -NoProfile -ExecutionPolicy Bypass -File $buildScript -OutputPath $targetExe

# 8. Create batch wrapper fallback
$cmdContent = "@echo off`r`n`"%~dp0..\runtime\venv\Scripts\python.exe`" `"%~dp0..\runtime\cx2\cx2_cli.py`" %*"
Set-Content -Path (Join-Path $TargetDir "bin\cx.cmd") -Value $cmdContent -Encoding ASCII

# 9. Update User PATH if needed
$binDir = Join-Path $TargetDir "bin"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathEntries = $userPath -split ";" | Where-Object { $_.Trim() -ne "" }
if ($pathEntries -notcontains $binDir) {
    Write-Host "[install] Adding $binDir to User PATH..." -ForegroundColor Cyan
    $newPath = ($pathEntries + $binDir) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    $env:Path = "$binDir;$env:Path"
}

# 10. Run doctor
Write-Host "`n[install] Running self-check..." -ForegroundColor Cyan
& $targetExe --doctor

Write-Host "`n=== CX2 Installation Complete ===" -ForegroundColor Green
Write-Host "You can now run 'cx' in your terminal."
