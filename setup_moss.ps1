# setup_moss.ps1 - MOSS-TTS-Nano isolated env (moss_env only)
# Usage: cd e:\alarm_speech ; .\setup_moss.ps1
# Needs Python 3.10+ (recommend 3.12) ONLY to create venv - does not change system pip

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$MossRepo = Join-Path $ProjectRoot "third_party\MOSS-TTS-Nano"
$MossEnv = Join-Path $ProjectRoot "moss_env"
$MossMarker = Join-Path $MossRepo "infer_onnx.py"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " MOSS-TTS-Nano setup (moss_env)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "All packages install ONLY inside moss_env." -ForegroundColor DarkGray
Write-Host ""

function Find-MossPython {
    $versions = @("3.12", "3.11", "3.10")
    foreach ($ver in $versions) {
        try {
            $path = & py "-$ver" -c "import sys; print(sys.executable)" 2>&1
            if ($LASTEXITCODE -eq 0 -and $path) {
                return $path.ToString().Trim()
            }
        }
        catch {
            # try next
        }
    }
    return $null
}

function Test-MossRepoReady {
    param([string]$RepoPath)
    return (Test-Path (Join-Path $RepoPath "infer_onnx.py"))
}

function Ensure-MossRepo {
    param([string]$RepoPath)

    if (Test-MossRepoReady -RepoPath $RepoPath) {
        Write-Host "[OK] Repo exists: $RepoPath" -ForegroundColor Green
        return
    }

    $parentDir = Split-Path $RepoPath -Parent
    New-Item -ItemType Directory -Force -Path $parentDir | Out-Null

    # Remove broken partial directory from a previous failed run
    if (Test-Path $RepoPath) {
        Write-Host "[WARN] Removing incomplete repo folder ..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force $RepoPath
    }

    # --- Method 1: git clone ---
    $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    if ($gitCmd) {
        Write-Host "[..] Cloning MOSS-TTS-Nano via git (may take a minute) ..." -ForegroundColor Yellow
        & git clone --depth 1 "https://github.com/OpenMOSS/MOSS-TTS-Nano.git" $RepoPath
        if (Test-MossRepoReady -RepoPath $RepoPath) {
            Write-Host "[OK] Repo cloned via git" -ForegroundColor Green
            return
        }
        Write-Host "[WARN] git clone finished but repo files missing, try ZIP ..." -ForegroundColor Yellow
        if (Test-Path $RepoPath) {
            Remove-Item -Recurse -Force $RepoPath
        }
    }
    else {
        Write-Host "[WARN] git not found, using ZIP download ..." -ForegroundColor Yellow
    }

    # --- Method 2: download ZIP (no git required) ---
    $zipUrl = "https://github.com/OpenMOSS/MOSS-TTS-Nano/archive/refs/heads/main.zip"
    $zipFile = Join-Path $env:TEMP "MOSS-TTS-Nano-main.zip"
    $extractDir = Join-Path $env:TEMP "MOSS-TTS-Nano-extract"

    Write-Host "[..] Downloading ZIP from GitHub ..." -ForegroundColor Yellow
    if (Test-Path $extractDir) {
        Remove-Item -Recurse -Force $extractDir
    }
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipFile -UseBasicParsing

    Write-Host "[..] Extracting ..." -ForegroundColor Yellow
    Expand-Archive -Path $zipFile -DestinationPath $extractDir -Force

    $extractedRepo = Join-Path $extractDir "MOSS-TTS-Nano-main"
    if (-not (Test-Path $extractedRepo)) {
        Write-Host "[ERROR] ZIP extract failed, folder not found: $extractedRepo" -ForegroundColor Red
        exit 1
    }

    Move-Item -Path $extractedRepo -Destination $RepoPath
    Remove-Item -Force $zipFile -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $extractDir -ErrorAction SilentlyContinue

    if (-not (Test-MossRepoReady -RepoPath $RepoPath)) {
        Write-Host "[ERROR] MOSS repo still incomplete after ZIP download." -ForegroundColor Red
        exit 1
    }

    Write-Host "[OK] Repo ready via ZIP download" -ForegroundColor Green
}

$MossPython = Find-MossPython
if (-not $MossPython) {
    Write-Host "[ERROR] Python 3.10+ not found (MOSS requires 3.10+)." -ForegroundColor Red
    Write-Host "Install Python 3.12 (side-by-side, will NOT replace 3.9):" -ForegroundColor Yellow
    Write-Host "  winget install Python.Python.3.12" -ForegroundColor White
    Write-Host "Then reopen terminal and run this script again." -ForegroundColor Yellow
    exit 1
}

Write-Host "[OK] Base Python for moss_env: $MossPython" -ForegroundColor Green

Ensure-MossRepo -RepoPath $MossRepo

if (-not (Test-MossRepoReady -RepoPath $MossRepo)) {
    Write-Host "[ERROR] MOSS repo path invalid: $MossRepo" -ForegroundColor Red
    exit 1
}

$venvPython = Join-Path $MossEnv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[..] Creating moss_env ..." -ForegroundColor Yellow
    & $MossPython -m venv $MossEnv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to create moss_env" -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] moss_env created" -ForegroundColor Green
}
else {
    Write-Host "[OK] moss_env already exists" -ForegroundColor Green
}

$VenvPython = Join-Path $MossEnv "Scripts\python.exe"
$VenvPip = Join-Path $MossEnv "Scripts\pip.exe"
$ReqFile = Join-Path $MossRepo "requirements.txt"

Write-Host "[..] Upgrading pip ..." -ForegroundColor Yellow
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "[..] Installing MOSS deps (torch/onnxruntime, may take a while) ..." -ForegroundColor Yellow

& $VenvPip install -r $ReqFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] Full requirements failed, retry without WeTextProcessing ..." -ForegroundColor Yellow
    $reqFiltered = Join-Path $env:TEMP "moss_req_no_wetext.txt"
    Get-Content $ReqFile | Where-Object { $_ -notmatch "WeTextProcessing" } | Set-Content $reqFiltered
    & $VenvPip install -r $reqFiltered
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] pip install requirements failed" -ForegroundColor Red
        exit 1
    }
}

& $VenvPip install -e $MossRepo
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] pip install -e . failed" -ForegroundColor Red
    exit 1
}

# Windows: pin compatible versions (onnxruntime 1.27 / sentencepiece 0.2.1 may crash on Win10)
Write-Host "[..] Applying Windows package pins ..." -ForegroundColor Yellow
& $VenvPip install "onnxruntime==1.20.1" "numpy<2" "sentencepiece==0.2.0"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Windows package pin failed" -ForegroundColor Red
    exit 1
}

$Cli = Join-Path $MossEnv "Scripts\moss-tts-nano.exe"
if (-not (Test-Path $Cli)) {
    Write-Host "[ERROR] moss-tts-nano CLI not found" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " moss_env ready!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "Test:  .\alarm_env\Scripts\python.exe test_moss_tts.py" -ForegroundColor Cyan
Write-Host "App:   .\run.ps1  -> select MOSS in UI" -ForegroundColor Cyan
Write-Host "Note: first TTS run downloads ~1GB models from HuggingFace" -ForegroundColor DarkGray
Write-Host ""
