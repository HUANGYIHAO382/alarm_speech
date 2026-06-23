# Windows compatibility pins for moss_env (run after setup_moss.ps1 if TTS fails)
# Fixes:
#   - onnxruntime 1.27 DLL load error -> pin 1.20.1
#   - sentencepiece 0.2.1 access violation on Windows -> pin 0.2.0
#   - numpy 2.x onnx issues -> pin numpy<2

$ErrorActionPreference = "Stop"
$VenvPip = Join-Path $PSScriptRoot "moss_env\Scripts\pip.exe"

if (-not (Test-Path $VenvPip)) {
    Write-Host "[ERROR] moss_env not found. Run setup_moss.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "[..] Applying Windows-compatible package pins ..." -ForegroundColor Yellow
& $VenvPip install "onnxruntime==1.20.1" "numpy<2" "sentencepiece==0.2.0"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] pip fix failed" -ForegroundColor Red
    exit 1
}

Write-Host "[..] Verifying imports ..." -ForegroundColor Yellow
$py = Join-Path $PSScriptRoot "moss_env\Scripts\python.exe"
$tok = Join-Path $PSScriptRoot "third_party\MOSS-TTS-Nano\models\MOSS-TTS-Nano-100M-ONNX\tokenizer.model"
& $py -c "import numpy, onnxruntime, sentencepiece as spm; p=spm.SentencePieceProcessor(); p.load(r'''$tok'''); print('OK', numpy.__version__, onnxruntime.__version__, p.vocab_size())"

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Verification failed" -ForegroundColor Red
    exit 1
}

Write-Host "[OK] moss_env Windows fixes applied." -ForegroundColor Green
Write-Host "Test: .\alarm_env\Scripts\python.exe test_moss_tts.py" -ForegroundColor Cyan
