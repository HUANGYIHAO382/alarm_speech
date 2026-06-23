# setup.ps1 - One-click project setup (isolated venvs only)
# Usage:
#   .\setup.ps1
#   .\setup.ps1 -WithMoss

param(
    [switch]$WithMoss
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

Write-Host ""
Write-Host "########################################" -ForegroundColor Magenta
Write-Host " alarm_speech setup" -ForegroundColor Magenta
Write-Host " Isolated venvs - no system Python change" -ForegroundColor Magenta
Write-Host "########################################" -ForegroundColor Magenta
Write-Host ""

& (Join-Path $ProjectRoot "setup_alarm.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($WithMoss) {
    Write-Host ""
    Write-Host "----------------------------------------" -ForegroundColor DarkGray
    & (Join-Path $ProjectRoot "setup_moss.ps1")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
else {
    Write-Host ""
    Write-Host "Skipped MOSS. Run: .\setup.ps1 -WithMoss" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Done. Start app: .\run.ps1" -ForegroundColor Green
Write-Host ""
