# run.ps1 - Start app using alarm_env only
$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot "alarm_env\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "[ERROR] alarm_env not found. Run: .\setup.ps1" -ForegroundColor Red
    exit 1
}

& $VenvPython (Join-Path $ProjectRoot "flet_demo.py")
