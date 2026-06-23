# setup_alarm.ps1 - Create isolated alarm_env (does NOT touch system Python)
# Usage: cd e:\alarm_speech ; .\setup_alarm.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$AlarmEnv = Join-Path $ProjectRoot "alarm_env"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " alarm_speech - alarm_env setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Packages install ONLY inside alarm_env folder." -ForegroundColor DarkGray
Write-Host ""

function Find-AlarmPython {
    $versions = @("3.9", "3.10", "3.11", "3.12", "3")
    foreach ($ver in $versions) {
        $pyExe = $null
        try {
            $pyExe = & py "-$ver" -c "import sys; print(sys.executable)" 2>&1
            if ($LASTEXITCODE -eq 0 -and $pyExe) {
                return $pyExe.ToString().Trim()
            }
        }
        catch {
            # try next version
        }
    }
    try {
        $pyExe = & python -c "import sys; print(sys.executable)" 2>&1
        if ($LASTEXITCODE -eq 0 -and $pyExe) {
            return $pyExe.ToString().Trim()
        }
    }
    catch {
        # ignore
    }
    return $null
}

$pythonPath = Find-AlarmPython
if (-not $pythonPath) {
    Write-Host "[ERROR] Python 3.8+ not found. Please install Python first." -ForegroundColor Red
    exit 1
}

Write-Host "[OK] Base Python for venv: $pythonPath" -ForegroundColor Green
Write-Host "     (only used to create venv, no global pip install)" -ForegroundColor DarkGray

$venvPython = Join-Path $AlarmEnv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[..] Creating alarm_env ..." -ForegroundColor Yellow
    & $pythonPath -m venv $AlarmEnv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to create alarm_env" -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] alarm_env created" -ForegroundColor Green
}
else {
    Write-Host "[OK] alarm_env already exists" -ForegroundColor Green
}

$VenvPython = Join-Path $AlarmEnv "Scripts\python.exe"
$VenvPip = Join-Path $AlarmEnv "Scripts\pip.exe"

Write-Host "[..] Upgrading pip inside alarm_env ..." -ForegroundColor Yellow
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "[..] Installing requirements.txt ..." -ForegroundColor Yellow
& $VenvPip install -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] pip install failed" -ForegroundColor Red
    exit 1
}

Write-Host "[..] Verifying flet + pywin32 ..." -ForegroundColor Yellow
& $VenvPython -c "import flet; import win32com.client; print('OK')"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Verification failed" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " alarm_env ready!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "Run app:  .\run.ps1" -ForegroundColor Cyan
Write-Host "MOSS TTS: .\setup_moss.ps1" -ForegroundColor Cyan
Write-Host ""
