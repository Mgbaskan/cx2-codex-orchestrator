[CmdletBinding()]
param(
    [string]$OutputPath = "$PSScriptRoot\..\bin\cx.exe"
)

$ErrorActionPreference = "Stop"

$launcherSource = Join-Path $PSScriptRoot "..\launcher\cx-launcher.cs"
if (-not (Test-Path $launcherSource)) {
    throw "Launcher source not found at: $launcherSource"
}

$outDir = Split-Path $OutputPath -Parent
if (-not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$frameworkDir = [System.Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()
$csc = Join-Path $frameworkDir "csc.exe"

if (-not (Test-Path $csc)) {
    throw "C# compiler (csc.exe) not found in .NET framework directory: $frameworkDir"
}

Write-Host "[build] Compiling cx launcher..." -ForegroundColor Cyan
& $csc /nologo /optimize+ /target:exe /out:"$OutputPath" "$launcherSource"

if ($LASTEXITCODE -ne 0) {
    throw "Compilation failed with exit code $LASTEXITCODE"
}

Write-Host "[build] Successfully compiled launcher to: $OutputPath" -ForegroundColor Green
