# setup_all.ps1 - 一键安装：虚拟环境 + MOSS 源码 + 模型下载
# ============================================================
# 别人 clone 项目后，在项目根目录执行本脚本即可完成全部准备工作。
#
# 用法:
#   .\setup_all.ps1                  # 完整安装（推荐）
#   .\setup_all.ps1 -UseMirror       # 使用国内 HF 镜像下载模型
#   .\setup_all.ps1 -SkipMoss        # 仅主程序环境（不用 MOSS 本地语音）
#   .\setup_all.ps1 -SkipModels      # 装环境但不预下载模型（首次播报时再下）
#   .\setup_all.ps1 -WithGpu         # 额外安装 MOSS GPU 加速（需 NVIDIA 显卡）
#
# 完成后启动: .\run.ps1
# 注意: 本文件须保存为 UTF-8 BOM，否则 Windows PowerShell 5.1 中文会乱码

param(
    # 跳过 MOSS 相关步骤（只用 Windows SAPI / 讯飞时可加此参数）
    [switch]$SkipMoss,
    # 跳过模型预下载（首次使用 MOSS 时会自动下载）
    [switch]$SkipModels,
    # 使用 Hugging Face 国内镜像加速模型下载
    [switch]$UseMirror,
    # 安装 onnxruntime-gpu 与 NVIDIA CUDA 运行时（体积约 1.5GB+）
    [switch]$WithGpu
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

# ---------- 路径常量 ----------
$AlarmPython  = Join-Path $ProjectRoot "alarm_env\Scripts\python.exe"
$MossPython   = Join-Path $ProjectRoot "moss_env\Scripts\python.exe"
$MossPip      = Join-Path $ProjectRoot "moss_env\Scripts\pip.exe"
$DownloadPy   = Join-Path $ProjectRoot "download_moss_models.py"
$ConfigLocal  = Join-Path $ProjectRoot "config.local.json"
$ConfigExample = Join-Path $ProjectRoot "config.local.example.json"

# ---------- 内部函数：从示例复制 config.local.json ----------
function Invoke-ConfigBootstrap {
    if (Test-Path $ConfigLocal) {
        # 单引号字符串：避免 PowerShell 把 [OK] 解析成类型/数组语法
        Write-Host '[OK] config.local.json 已存在，跳过复制' -ForegroundColor DarkGray
        return
    }
    if (-not (Test-Path $ConfigExample)) {
        Write-Host '[WARN] 未找到 config.local.example.json' -ForegroundColor Yellow
        return
    }
    Copy-Item -Path $ConfigExample -Destination $ConfigLocal
    Write-Host '[OK] 已从模板创建 config.local.json（可按需填写讯飞密钥）' -ForegroundColor Green
}

Write-Host ""
Write-Host "########################################" -ForegroundColor Magenta
Write-Host " alarm_speech 一键安装" -ForegroundColor Magenta
Write-Host " 虚拟环境 + MOSS + 模型（全自动）" -ForegroundColor Magenta
Write-Host "########################################" -ForegroundColor Magenta
Write-Host ""

# ============================================================
# 步骤 1：主程序虚拟环境 alarm_env
# ============================================================
Write-Host '[1/5] 安装主程序环境 (alarm_env) ...' -ForegroundColor Cyan
& (Join-Path $ProjectRoot "setup_alarm.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($SkipMoss) {
    Write-Host ""
    Write-Host '[跳过] MOSS 相关步骤（-SkipMoss）' -ForegroundColor DarkGray
    Invoke-ConfigBootstrap
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Green
    Write-Host " 安装完成（仅主程序）" -ForegroundColor Green
    Write-Host " 启动: .\run.ps1" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Green
    Write-Host ""
    exit 0
}

# ============================================================
# 步骤 2：MOSS 虚拟环境 + 克隆上游仓库
# ============================================================
Write-Host ""
Write-Host '[2/5] 安装 MOSS 环境 (moss_env) ...' -ForegroundColor Cyan
& (Join-Path $ProjectRoot "setup_moss.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# ============================================================
# 步骤 3：安装 huggingface_hub（模型下载用）
# ============================================================
Write-Host ""
Write-Host '[3/5] 安装模型下载依赖 (huggingface_hub) ...' -ForegroundColor Cyan
if (-not (Test-Path $MossPip)) {
    Write-Host '[ERROR] moss_env 未找到，setup_moss.ps1 可能失败' -ForegroundColor Red
    exit 1
}
& $MossPip install "huggingface_hub>=0.20.0"
if ($LASTEXITCODE -ne 0) {
    Write-Host '[ERROR] huggingface_hub 安装失败' -ForegroundColor Red
    exit 1
}
Write-Host '[OK] huggingface_hub 已安装' -ForegroundColor Green

# ============================================================
# 步骤 4：预下载 MOSS ONNX 模型（约 1GB）
# ============================================================
if ($SkipModels) {
    Write-Host ""
    Write-Host '[跳过] 模型预下载（-SkipModels），首次 MOSS 播报时会自动下载' -ForegroundColor DarkGray
}
else {
    Write-Host ""
    Write-Host '[4/5] 预下载 MOSS 模型（约 1GB，请耐心等待） ...' -ForegroundColor Cyan
    if ($UseMirror) {
        $env:HF_ENDPOINT = "https://hf-mirror.com"
        Write-Host "      使用镜像: $env:HF_ENDPOINT" -ForegroundColor DarkGray
    }
    $dlArgs = @($DownloadPy)
    if ($UseMirror) { $dlArgs += "--mirror" }
    & $MossPython @dlArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[WARN] 模型下载失败，可稍后重试:' -ForegroundColor Yellow
        Write-Host "       .\moss_env\Scripts\python.exe download_moss_models.py --mirror" -ForegroundColor White
    }
}

# ============================================================
# 步骤 5：Windows 兼容修复 + 可选 GPU
# ============================================================
Write-Host ""
Write-Host '[5/5] 应用 Windows 兼容修复 ...' -ForegroundColor Cyan

# fix_moss_win 会校验 tokenizer.model，仅在模型已下载时执行
$modelsRoot = Join-Path $ProjectRoot "third_party\MOSS-TTS-Nano\models"
$manifest = Get-ChildItem -Path $modelsRoot -Recurse -Filter "browser_poc_manifest.json" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($manifest) {
    & (Join-Path $ProjectRoot "fix_moss_win.ps1")
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[WARN] fix_moss_win 未完全通过，可稍后重试' -ForegroundColor Yellow
    }
}
else {
    Write-Host '[跳过] 模型未就绪，跳过 tokenizer 校验（下载成功后可运行 fix_moss_win.ps1）' -ForegroundColor DarkGray
}

if ($WithGpu) {
    Write-Host ""
    Write-Host '[可选] 安装 MOSS GPU 加速 ...' -ForegroundColor Cyan
    & (Join-Path $ProjectRoot "fix_moss_gpu.ps1")
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[WARN] GPU 安装未成功，可继续使用 CPU 模式' -ForegroundColor Yellow
    }
}

# ============================================================
# 生成本地配置模板（不含密钥）
# ============================================================
Invoke-ConfigBootstrap

# ============================================================
# 完成提示
# ============================================================
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " 全部安装完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "启动程序:     .\run.ps1" -ForegroundColor Cyan
Write-Host "测试 MOSS:    .\alarm_env\Scripts\python.exe test_moss_tts.py" -ForegroundColor Cyan
Write-Host "界面引擎:     选择「MOSS 本地」" -ForegroundColor DarkGray
Write-Host ""
Write-Host "可选配置:     编辑 config.local.json（讯飞密钥等）" -ForegroundColor DarkGray
Write-Host ""
