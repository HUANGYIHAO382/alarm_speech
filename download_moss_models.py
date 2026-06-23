"""
MOSS ONNX 模型预下载脚本
========================
从 Hugging Face 拉取 MOSS-TTS-Nano 所需的 ONNX 权重到本地，
避免用户第一次打开程序时才长时间等待。

用法（推荐在 moss_env 中运行，setup_all.ps1 会自动调用）:
  moss_env\\Scripts\\python.exe download_moss_models.py
  moss_env\\Scripts\\python.exe download_moss_models.py --mirror   # 国内镜像

依赖:
  - 已执行 setup_moss.ps1（third_party/MOSS-TTS-Nano 存在）
  - moss_env 中已安装 huggingface_hub（setup_all.ps1 会自动安装）
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


# ---------- 项目路径 ----------
# 脚本位于项目根目录，MOSS 源码默认在 third_party 下
PROJECT_ROOT = Path(__file__).resolve().parent
MOSS_REPO = PROJECT_ROOT / "third_party" / "MOSS-TTS-Nano"
MODELS_DIR = MOSS_REPO / "models"

# Hugging Face 上的两个 ONNX 模型仓库（与 MOSS 上游 onnx_tts_runtime.py 一致）
TTS_REPO_ID = "OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX"
CODEC_REPO_ID = "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX"

# 本地存放目录名
TTS_LOCAL_NAME = "MOSS-TTS-Nano-100M-ONNX"
CODEC_LOCAL_NAME = "MOSS-Audio-Tokenizer-Nano-ONNX"

# 用于判断模型是否已完整的标志文件
TTS_MANIFEST = "browser_poc_manifest.json"
CODEC_META = "codec_browser_onnx_meta.json"


def _apply_mirror(use_mirror: bool) -> None:
    """若指定镜像，设置 HF_ENDPOINT 环境变量（huggingface_hub 会读取）。"""
    if use_mirror:
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        print("[镜像] HF_ENDPOINT=https://hf-mirror.com")
    elif os.environ.get("HF_ENDPOINT"):
        print(f"[镜像] 使用已有 HF_ENDPOINT={os.environ['HF_ENDPOINT']}")


def _models_ready() -> bool:
    """检查 TTS 与 Codec 模型文件是否已在本地。"""
    tts_dir = MODELS_DIR / TTS_LOCAL_NAME
    codec_dir = MODELS_DIR / CODEC_LOCAL_NAME
    tts_ok = (tts_dir / TTS_MANIFEST).is_file() or _find_file(tts_dir, TTS_MANIFEST)
    codec_ok = (codec_dir / CODEC_META).is_file() or _find_file(codec_dir, CODEC_META)
    return tts_ok and codec_ok


def _find_file(root: Path, name: str) -> bool:
    """在目录树中查找指定文件名（下载后可能多一层子目录）。"""
    if not root.is_dir():
        return False
    return any(root.rglob(name))


def _ensure_huggingface_hub() -> None:
    """确保 huggingface_hub 已安装（用于 snapshot_download）。"""
    try:
        import huggingface_hub  # noqa: F401
    except ModuleNotFoundError:
        print("[错误] 未安装 huggingface_hub，请先运行: .\\setup_all.ps1")
        sys.exit(1)


def _download_one_repo(repo_id: str, local_dir: Path, patterns: tuple[str, ...]) -> None:
    """从 Hugging Face 下载单个模型仓库到 local_dir。"""
    from huggingface_hub import snapshot_download

    print(f"[下载] {repo_id}")
    print(f"       -> {local_dir}")
    local_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo_id,
        local_dir=str(local_dir),
        local_dir_use_symlinks=False,
        allow_patterns=list(patterns),
    )


def _normalize_layout(target_dir: Path, required_names: tuple[str, ...]) -> None:
    """
    若下载结果多嵌套了一层目录，把文件提升到 target_dir 根下。
    逻辑与 MOSS 上游 onnx_tts_runtime._normalize_download_layout 类似。
    """
    import shutil

    if not target_dir.is_dir():
        return

    # 若根目录已有全部必需文件，无需整理
    if all((target_dir / name).exists() for name in required_names):
        return

    sentinel = required_names[0]
    for candidate in target_dir.rglob(sentinel):
        parent = candidate.parent
        if all((parent / name).exists() for name in required_names):
            for child in parent.iterdir():
                dest = target_dir / child.name
                if not dest.exists():
                    shutil.move(str(child), str(dest))
            return


def download_models(*, force: bool = False, use_mirror: bool = False) -> None:
    """主流程：检查 -> 下载 TTS + Codec -> 验证。"""
    if not (MOSS_REPO / "infer_onnx.py").is_file():
        print(f"[错误] MOSS 仓库不完整，请先运行: .\\setup_moss.ps1")
        print(f"       期望路径: {MOSS_REPO}")
        sys.exit(1)

    _apply_mirror(use_mirror)
    _ensure_huggingface_hub()

    if not force and _models_ready():
        print("[跳过] 模型已存在，无需重复下载。")
        print(f"       目录: {MODELS_DIR}")
        return

    print("")
    print("=" * 60)
    print(" MOSS ONNX 模型下载（约 1 GB，请保持网络畅通）")
    print("=" * 60)
    print("")

    tts_dir = MODELS_DIR / TTS_LOCAL_NAME
    codec_dir = MODELS_DIR / CODEC_LOCAL_NAME

    # TTS 主模型：onnx 权重 + manifest + 分词器
    _download_one_repo(
        TTS_REPO_ID,
        tts_dir,
        ("*.onnx", "*.data", "*.json", "tokenizer.model"),
    )
    _normalize_layout(tts_dir, (TTS_MANIFEST, "tts_browser_onnx_meta.json", "tokenizer.model"))

    # 音频 Codec 模型
    _download_one_repo(
        CODEC_REPO_ID,
        codec_dir,
        ("*.onnx", "*.data", "*.json"),
    )
    _normalize_layout(codec_dir, (CODEC_META,))

    if not _models_ready():
        print("[错误] 下载完成但校验失败，请检查网络或重试 --force")
        sys.exit(1)

    print("")
    print("[完成] MOSS 模型已就绪:")
    print(f"       {MODELS_DIR}")
    print("")


def main() -> None:
    parser = argparse.ArgumentParser(description="预下载 MOSS-TTS-Nano ONNX 模型")
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使本地已有模型也重新下载",
    )
    parser.add_argument(
        "--mirror",
        action="store_true",
        help="使用 Hugging Face 国内镜像 (hf-mirror.com)",
    )
    args = parser.parse_args()
    download_models(force=args.force, use_mirror=args.mirror)


if __name__ == "__main__":
    main()
