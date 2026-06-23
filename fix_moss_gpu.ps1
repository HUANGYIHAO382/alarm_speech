# fix_moss_gpu.ps1 - Install onnxruntime-gpu + NVIDIA CUDA runtime wheels for RTX GPUs
# Run after setup_moss.ps1 / fix_moss_win.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$VenvPip = Join-Path $ProjectRoot "moss_env\Scripts\pip.exe"
$VenvPython = Join-Path $ProjectRoot "moss_env\Scripts\python.exe"
$CudaProbe = Join-Path $ProjectRoot "moss_cuda_probe.py"

if (-not (Test-Path $VenvPip)) {
    Write-Host "[ERROR] moss_env not found. Run setup_moss.ps1 first." -ForegroundColor Red
    exit 1
}

function Invoke-CudaProbe {
    # onnxruntime 会把 Warning 写到 stderr；在 Stop 模式下 PowerShell 会误报为错误
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $stdout = & $VenvPython $CudaProbe 2>$null
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $prev
    return @{
        ExitCode = $exitCode
        Output   = ($stdout | Out-String).Trim()
    }
}

function Test-CudaReallyWorks {
    $result = Invoke-CudaProbe
    return ($result.ExitCode -eq 0) -and ($result.Output -eq "OK")
}

function Install-CpuFallback {
    Write-Host "[..] Rolling back to CPU onnxruntime 1.20.1 ..." -ForegroundColor Yellow
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $VenvPip uninstall -y onnxruntime-gpu 2>&1 | Out-Null
    $ErrorActionPreference = $prev
    & $VenvPip install "onnxruntime==1.20.1" "numpy<2" "sentencepiece==0.2.0"
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " MOSS GPU (CUDA) setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

Write-Host "[..] Removing old onnxruntime packages ..." -ForegroundColor Yellow
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $VenvPip uninstall -y onnxruntime onnxruntime-gpu 2>&1 | Out-Null
$ErrorActionPreference = $prevEap

Write-Host "[..] Installing onnxruntime-gpu 1.20.1 (~280MB) ..." -ForegroundColor Yellow
& $VenvPip install "onnxruntime-gpu==1.20.1" "numpy<2" "sentencepiece==0.2.0"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] pip install onnxruntime-gpu failed" -ForegroundColor Red
    Install-CpuFallback
    exit 1
}

Write-Host "[..] Installing NVIDIA CUDA runtime wheels (cuDNN / cuBLAS / ...) ..." -ForegroundColor Yellow
Write-Host "     This may download ~1.5GB. Please wait." -ForegroundColor DarkGray
& $VenvPip install `
    "nvidia-cudnn-cu12" `
    "nvidia-cublas-cu12" `
    "nvidia-cuda-runtime-cu12" `
    "nvidia-nvjitlink-cu12" `
    "nvidia-cufft-cu12" `
    "nvidia-curand-cu12" `
    "nvidia-cusolver-cu12" `
    "nvidia-cusparse-cu12"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] Some NVIDIA wheels failed to install." -ForegroundColor Yellow
}

Write-Host "[..] Verifying real CUDA session (not just provider list) ..." -ForegroundColor Yellow
$probeResult = Invoke-CudaProbe
Write-Host "Probe exit=$($probeResult.ExitCode) output=$($probeResult.Output)" -ForegroundColor DarkGray

if ($probeResult.ExitCode -ne 0 -or $probeResult.Output -ne "OK") {
    Write-Host "[WARN] CUDA session probe failed." -ForegroundColor Yellow
    Write-Host "       GPU mode may auto-fallback to CPU in the app." -ForegroundColor Yellow
    Write-Host "       Check logs/moss_daemon.log after preview." -ForegroundColor Yellow
    Write-Host "       Ensure: latest NVIDIA driver + Visual C++ 2015-2022 x64 redistributable." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "[OK] onnxruntime-gpu installed; CPU fallback remains available." -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host "[OK] GPU acceleration verified!" -ForegroundColor Green
Write-Host "Restart app: .\run.ps1" -ForegroundColor Cyan
Write-Host "In UI: Voice engine -> MOSS -> MOSS accel -> GPU (CUDA)" -ForegroundColor Cyan
Write-Host ""
