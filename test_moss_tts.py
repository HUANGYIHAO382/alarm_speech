"""
MOSS-TTS-Nano 语音播报 — 独立测试脚本
======================================
不依赖 Flet，用于验证 MOSS 环境与模型是否就绪。

用法:
  python test_moss_tts.py                    # 默认测试句
  python test_moss_tts.py -t "自定义文字"     # 指定播报内容
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from tts_config import create_moss_client, is_moss_configured, get_moss_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="MOSS-TTS-Nano 语音测试")
    parser.add_argument(
        "-t", "--text",
        default="你好，这是 MOSS 本地语音模型测试。",
        help="要合成的文字",
    )
    args = parser.parse_args()

    if not is_moss_configured():
        settings = get_moss_settings()
        print("MOSS 环境未就绪。")
        print(f"  Python: {settings['python']}")
        print(f"  仓库:   {settings['repo_dir']}")
        print()
        print("请先运行: .\\setup_moss.ps1")
        sys.exit(1)

    print("正在合成（首次运行会下载模型，可能需要几分钟）...")
    client = create_moss_client()
    wav_bytes = client.synthesize_for_playback(args.text)
    print(f"合成成功，WAV 大小: {len(wav_bytes)} 字节")

    # 写入临时文件并播放
    fd, path = tempfile.mkstemp(suffix=".wav")
    Path(path).write_bytes(wav_bytes)
    print(f"播放中: {path}")
    try:
        import winsound
        # SND_SYNC: wait until playback finishes before deleting temp file
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_SYNC)
    finally:
        Path(path).unlink(missing_ok=True)
    print("完成。")


if __name__ == "__main__":
    main()
