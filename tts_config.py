"""
TTS 配置加载 (合并 config.json + config.local.json + 环境变量)
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict

MOSS_DEVICE_CPU = "cpu"
MOSS_DEVICE_GPU = "cuda"

MOSS_DEVICE_LABELS = {
    MOSS_DEVICE_CPU: "纯 CPU",
    MOSS_DEVICE_GPU: "GPU 加速 (CUDA)",
}

# MOSS 纯 CPU 模式下的 ONNX 算子内并行线程（intra_op，非多句并行）
MOSS_CPU_PRESET_RESERVE1 = "reserve1"
MOSS_CPU_PRESET_ALL = "all"

MOSS_CPU_THREAD_PRESETS = [
    (MOSS_CPU_PRESET_RESERVE1, "自动（留 1 核）"),
    (MOSS_CPU_PRESET_ALL, "全部逻辑核"),
    ("2", "2 线程"),
    ("4", "4 线程"),
    ("6", "6 线程"),
    ("8", "8 线程"),
    ("12", "12 线程"),
    ("16", "16 线程"),
]


def logical_cpu_count() -> int:
    """本机逻辑 CPU 核数（含超线程）。"""
    return max(1, int(os.cpu_count() or 4))


def resolve_moss_cpu_threads(preset: str | int | None = None) -> int:
    """
    将 UI 预设转为 moss_daemon 的 --cpu-threads 数值。
    reserve1 = 逻辑核数 - 1（推荐，给系统与 Flet UI 留余量）。
    """
    if preset is None:
        preset = MOSS_CPU_PRESET_RESERVE1
    key = str(preset).strip().lower()
    if key == MOSS_CPU_PRESET_RESERVE1:
        return max(1, logical_cpu_count() - 1)
    if key == MOSS_CPU_PRESET_ALL:
        return logical_cpu_count()
    try:
        return max(1, int(key))
    except ValueError:
        return max(1, logical_cpu_count() - 1)


def moss_cpu_threads_label(threads: int) -> str:
    """状态栏展示用，例如「7 线程（留 1 核）」。"""
    total = logical_cpu_count()
    if threads >= total:
        return f"{threads} 线程（全部逻辑核）"
    if threads == max(1, total - 1):
        return f"{threads} 线程（留 1 核）"
    return f"{threads} 线程"


def project_root() -> Path:
    return Path(__file__).resolve().parent


def read_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_merged_config() -> Dict[str, Any]:
    root = project_root()
    base = read_config(root / "config.json")
    local = read_config(root / "config.local.json")
    return _deep_merge(base, local)


def get_xfyun_settings(config: dict | None = None) -> dict:
    cfg = config if config is not None else load_merged_config()
    tts = cfg.get("tts") or {}
    xf = tts.get("xfyun") or {}
    return {
        "app_id": (
            os.environ.get("XFYUN_APP_ID", "").strip()
            or str(xf.get("app_id", "")).strip()
        ),
        "api_key": (
            os.environ.get("XFYUN_API_KEY", "").strip()
            or str(xf.get("api_key", "")).strip()
        ),
        "api_secret": (
            os.environ.get("XFYUN_API_SECRET", "").strip()
            or str(xf.get("api_secret", "")).strip()
        ),
        "host_url": xf.get("host_url", "wss://tts-api.xfyun.cn/v2/tts"),
        "vcn": xf.get("vcn", "xiaoyan"),
        "auf": xf.get("auf", "audio/L16;rate=16000"),
        "speed": int(xf.get("speed", 50)),
        "volume": int(xf.get("volume", 50)),
        "pitch": int(xf.get("pitch", 50)),
        "tte": xf.get("tte", "UTF8"),
        "sample_rate": int(xf.get("sample_rate", 16000)),
    }


def create_xfyun_client(config: dict | None = None):
    from xfyun_tts import XfyunTtsClient

    s = get_xfyun_settings(config)
    return XfyunTtsClient(
        app_id=s["app_id"],
        api_key=s["api_key"],
        api_secret=s["api_secret"],
        host_url=s["host_url"],
        vcn=s["vcn"],
        auf=s["auf"],
        speed=s["speed"],
        volume=s["volume"],
        pitch=s["pitch"],
        tte=s["tte"],
        sample_rate=s["sample_rate"],
    )


def default_moss_python() -> Path:
    """MOSS 独立虚拟环境的 Python 路径（与主程序 alarm_env 分离）。"""
    return project_root() / "moss_env" / "Scripts" / "python.exe"


def default_moss_repo_dir() -> Path:
    """MOSS-TTS-Nano 源码默认克隆位置。"""
    return project_root() / "third_party" / "MOSS-TTS-Nano"


def _resolve_moss_python(cfg: dict) -> str:
    moss = (cfg.get("tts") or {}).get("moss") or {}
    return (
        os.environ.get("MOSS_PYTHON", "").strip()
        or str(moss.get("python", "")).strip()
        or str(default_moss_python())
    )


def _probe_moss_cuda_python(python_exe: str) -> bool:
    """通过子进程真实创建 CUDA Session，避免仅检查 provider 列表误报。"""
    py = Path(python_exe)
    if not py.is_file():
        return False
    probe_script = project_root() / "moss_cuda_probe.py"
    if not probe_script.is_file():
        return False
    try:
        result = subprocess.run(
            [str(py), str(probe_script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=__import__("moss_cuda_env", fromlist=["build_subprocess_env"]).build_subprocess_env(py),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0 and result.stdout.strip() == "OK"
    except Exception:
        return False


def probe_moss_cuda_detailed(config: dict | None = None) -> tuple[bool, str]:
    """返回 (CUDA 是否真正可用, 说明)。"""
    cfg = config if config is not None else load_merged_config()
    py = _resolve_moss_python(cfg)
    if not Path(py).is_file():
        return False, "moss_env 未安装"
    try:
        from moss_cuda_env import build_subprocess_env

        result = subprocess.run(
            [py, str(project_root() / "moss_cuda_probe.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=build_subprocess_env(py),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            return True, "CUDA 可用"
        detail = (result.stdout or result.stderr or "").strip()
        return False, detail or "CUDA 不可用"
    except Exception as err:
        return False, str(err)


def detect_moss_cuda_available(config: dict | None = None) -> bool:
    """检测 moss_env 是否已安装 onnxruntime-gpu 且 CUDA 可用。"""
    cfg = config if config is not None else load_merged_config()
    return _probe_moss_cuda_python(_resolve_moss_python(cfg))


def default_moss_execution_provider(config: dict | None = None) -> str:
    """有显卡且 onnxruntime-gpu 就绪时默认 GPU。"""
    cfg = config if config is not None else load_merged_config()
    moss = (cfg.get("tts") or {}).get("moss") or {}
    explicit = (
        os.environ.get("MOSS_EXECUTION_PROVIDER", "").strip().lower()
        or str(moss.get("execution_provider", "")).strip().lower()
    )
    if explicit in (MOSS_DEVICE_CPU, MOSS_DEVICE_GPU):
        return explicit
    return MOSS_DEVICE_GPU if _probe_moss_cuda_python(_resolve_moss_python(cfg)) else MOSS_DEVICE_CPU


def get_moss_settings(config: dict | None = None) -> dict:
    """读取 MOSS-TTS-Nano 相关配置。"""
    cfg = config if config is not None else load_merged_config()
    tts = cfg.get("tts") or {}
    moss = tts.get("moss") or {}

    repo_dir = (
        os.environ.get("MOSS_REPO_DIR", "").strip()
        or str(moss.get("repo_dir", "")).strip()
        or str(default_moss_repo_dir())
    )
    python_exe = (
        os.environ.get("MOSS_PYTHON", "").strip()
        or str(moss.get("python", "")).strip()
        or str(default_moss_python())
    )

    # 默认参考音频：MOSS 仓库自带的 zh_1.wav（音色克隆用，可选）
    default_prompt = str(Path(repo_dir) / "assets" / "audio" / "zh_1.wav")

    return {
        "python": python_exe,
        "repo_dir": repo_dir,
        "backend": str(moss.get("backend", "onnx")).strip().lower(),
        "voice": str(moss.get("voice", "Junhao")).strip(),
        "prompt_audio_path": (
            os.environ.get("MOSS_PROMPT_AUDIO", "").strip()
            or str(moss.get("prompt_audio_path", "")).strip()
            or default_prompt
        ),
        "model_dir": str(moss.get("model_dir", "")).strip(),
        "cpu_threads": int(moss.get("cpu_threads", 4)),
        "execution_provider": default_moss_execution_provider(cfg),
        "daemon_port": int(moss.get("daemon_port", 18764)),
    }


def is_moss_configured(config: dict | None = None) -> bool:
    """MOSS 环境是否已安装（Python + 仓库 + CLI）。"""
    s = get_moss_settings(config)
    python_path = Path(s["python"])
    repo_path = Path(s["repo_dir"])
    if not python_path.is_file():
        return False
    if not (repo_path / "infer_onnx.py").is_file():
        return False
    scripts_dir = python_path.parent
    return (scripts_dir / "moss-tts-nano.exe").is_file() or (
        scripts_dir / "moss-tts-nano"
    ).is_file()


def create_moss_client(
    config: dict | None = None,
    execution_provider: str | None = None,
    cpu_threads: int | None = None,
):
    from moss_tts import MossTtsClient

    s = get_moss_settings(config)
    if execution_provider:
        s = {**s, "execution_provider": execution_provider.strip().lower()}
    if cpu_threads is not None:
        s = {**s, "cpu_threads": max(1, int(cpu_threads))}
    return MossTtsClient(
        python_exe=s["python"],
        repo_dir=s["repo_dir"],
        backend=s["backend"],
        voice=s["voice"],
        prompt_audio_path=s["prompt_audio_path"],
        model_dir=s["model_dir"],
        cpu_threads=s["cpu_threads"],
        execution_provider=s["execution_provider"],
        daemon_port=s["daemon_port"],
    )
