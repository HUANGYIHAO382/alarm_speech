"""
MOSS CUDA 运行环境辅助
======================
Windows 下 onnxruntime-gpu 需要把 NVIDIA / ORT 的 DLL 目录加入搜索路径，
否则会出现 error 126，UI 侧进度条一直停在「等待模型加载 0.0s」。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, List, Tuple


def site_packages_for_python(python_exe: str | Path | None = None) -> Path:
    """根据 moss_env 的 python.exe 推算 site-packages 目录。"""
    if python_exe:
        py = Path(python_exe).resolve()
        return py.parent.parent / "Lib" / "site-packages"
    return Path(sys.prefix) / "Lib" / "site-packages"


def collect_cuda_dll_dirs(site_packages: Path) -> List[str]:
    """收集需要加入 DLL 搜索路径的目录。"""
    dirs: List[str] = []
    sp = site_packages.resolve()

    nvidia_root = sp / "nvidia"
    if nvidia_root.is_dir():
        for bin_dir in sorted(nvidia_root.rglob("bin")):
            if bin_dir.is_dir():
                dirs.append(str(bin_dir.resolve()))

    ort_capi = sp / "onnxruntime" / "capi"
    if ort_capi.is_dir():
        dirs.append(str(ort_capi.resolve()))

    # 去重且保持顺序
    seen = set()
    unique: List[str] = []
    for item in dirs:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def prepend_path(dirs: Iterable[str]) -> str:
    """把目录列表前置到 PATH，返回新的 PATH 前缀。"""
    prefix = os.pathsep.join(dirs)
    old = os.environ.get("PATH", "")
    if prefix and prefix not in old:
        os.environ["PATH"] = prefix + os.pathsep + old
    return prefix


def setup_cuda_dll_paths(
    site_packages: Path | None = None,
    *,
    python_exe: str | Path | None = None,
) -> List[str]:
    """
    在当前进程注册 CUDA 相关 DLL 目录（add_dll_directory + PATH）。
    应在 import onnxruntime 之前调用。
    """
    sp = site_packages or site_packages_for_python(python_exe)
    dirs = collect_cuda_dll_dirs(sp)
    for folder in dirs:
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(folder)
            except OSError:
                pass
    if dirs:
        prepend_path(dirs)
    return dirs


def build_subprocess_env(python_exe: str | Path) -> dict:
    """为 moss 子进程构造带 CUDA DLL 路径的环境变量。"""
    env = os.environ.copy()
    dirs = collect_cuda_dll_dirs(site_packages_for_python(python_exe))
    if dirs:
        prefix = os.pathsep.join(dirs)
        env["PATH"] = prefix + os.pathsep + env.get("PATH", "")
    return env


def default_moss_repo_dir() -> Path:
    return Path(__file__).resolve().parent / "third_party" / "MOSS-TTS-Nano"


def probe_cuda_ready(
    repo_dir: Path | None = None,
    *,
    python_exe: str | Path | None = None,
) -> Tuple[bool, str]:
    """
    真实探测 CUDA 是否可用：创建 ORT Session 并确认实际 provider 含 CUDA。
    返回 (是否可用, 说明文字)。
    """
    setup_cuda_dll_paths(python_exe=python_exe)

    try:
        import onnxruntime as ort
    except ImportError:
        return False, "未安装 onnxruntime"

    listed = ort.get_available_providers()
    if "CUDAExecutionProvider" not in listed:
        return False, f"未列出 CUDA provider（当前: {listed}）"

    repo = repo_dir or default_moss_repo_dir()
    model = repo / "models" / "MOSS-TTS-Nano-100M-ONNX" / "moss_tts_prefill.onnx"
    if not model.is_file():
        return False, f"探测模型不存在: {model}"

    try:
        session_options = ort.SessionOptions()
        # 屏蔽 ORT 写到 stderr 的性能警告，避免 PowerShell 误判为脚本失败
        session_options.log_severity_level = 3
        session = ort.InferenceSession(
            str(model),
            sess_options=session_options,
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        active = session.get_providers()
        if "CUDAExecutionProvider" in active:
            return True, "CUDA 会话创建成功"
        return False, (
            "CUDA provider 已安装但会话回退到 CPU。"
            "请运行 fix_moss_gpu.ps1 安装 cuDNN 等依赖，或查看 logs/moss_daemon.log"
        )
    except Exception as err:
        return False, f"CUDA 探测失败: {err}"
