"""
MOSS-TTS-Nano 常驻守护进程
==========================
模型只加载一次，后续合成走 HTTP，避免每次试听都冷启动（卡顿主因）。

启动（由 moss_tts.py 自动拉起，也可手动）:
    moss_env\\Scripts\\python.exe moss_daemon.py --execution-provider cuda

接口:
    GET  /health   -> 是否就绪
    GET  /status   -> 加载/合成进度
    POST /synthesize -> { "text": "...", "voice": "Junhao" }
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# 将 MOSS 源码目录加入路径
_PROJECT_ROOT = Path(__file__).resolve().parent

# 在 import onnxruntime 之前注册 NVIDIA / ORT 的 DLL 搜索路径（Windows 必需）
from moss_cuda_env import setup_cuda_dll_paths

setup_cuda_dll_paths()
_MOSS_REPO = _PROJECT_ROOT / "third_party" / "MOSS-TTS-Nano"
if str(_MOSS_REPO) not in sys.path:
    sys.path.insert(0, str(_MOSS_REPO))

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="MOSS-TTS Daemon", version="1.0")

# 全局状态（供 /status 轮询）
_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "ready": False,
    "phase": "init",
    "message": "守护进程启动中",
    "execution_provider": "cpu",
    "started_at": time.time(),
    "last_elapsed_ms": 0,
}

_runtime = None


class SynthBody(BaseModel):
    text: str
    voice: str = "Junhao"


def _set_state(**kwargs) -> None:
    with _state_lock:
        _state.update(kwargs)


def _load_runtime(execution_provider: str, cpu_threads: int) -> None:
    """在启动时加载 ONNX 运行时（耗时操作，仅执行一次）。"""
    global _runtime
    from onnx_tts_runtime import OnnxTtsRuntime

    _set_state(
        phase="loading",
        message=f"加载 MOSS 模型 ({execution_provider.upper()})...",
        ready=False,
        execution_provider=execution_provider,
    )
    t0 = time.perf_counter()
    try:
        _runtime = OnnxTtsRuntime(
            thread_count=max(1, cpu_threads),
            execution_provider=execution_provider,
        )
    except Exception as err:
        # 失败时写入状态，避免 UI 无限轮询 0.0s
        _set_state(
            ready=False,
            phase="error",
            message=f"模型加载失败: {err}",
        )
        raise
    elapsed = int((time.perf_counter() - t0) * 1000)
    _set_state(
        ready=True,
        phase="ready",
        message=f"模型已就绪 ({elapsed} ms)",
        last_elapsed_ms=elapsed,
    )


@app.get("/health")
def health():
    return {"ok": _state.get("ready", False)}


@app.get("/status")
def status():
    with _state_lock:
        data = dict(_state)
    data["uptime_s"] = round(time.time() - float(data.get("started_at", time.time())), 1)
    return data


@app.post("/synthesize")
def synthesize(body: SynthBody):
    if _runtime is None or not _state.get("ready"):
        return JSONResponse({"ok": False, "error": "模型未就绪"}, status_code=503)

    text = (body.text or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "文本为空"}, status_code=400)

    _set_state(phase="synthesize", message="语音合成中...")
    t0 = time.perf_counter()
    fd, wav_path = tempfile.mkstemp(suffix=".moss_daemon.wav")
    import os
    os.close(fd)
    path = Path(wav_path)
    try:
        _runtime.synthesize(
            text=text,
            voice=body.voice,
            output_audio_path=str(path),
            enable_wetext=False,
        )
        wav_bytes = path.read_bytes()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        _set_state(
            phase="ready",
            message="合成完成",
            last_elapsed_ms=elapsed_ms,
        )
        return {
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "wav_b64": base64.b64encode(wav_bytes).decode("ascii"),
            "size": len(wav_bytes),
        }
    except Exception as err:
        _set_state(phase="error", message=str(err))
        return JSONResponse({"ok": False, "error": str(err)}, status_code=500)
    finally:
        path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="MOSS-TTS persistent daemon")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18764)
    parser.add_argument(
        "--execution-provider",
        choices=("cpu", "cuda"),
        default="cpu",
        help="cpu=纯CPU, cuda=使用显卡 (需 onnxruntime-gpu)",
    )
    parser.add_argument("--cpu-threads", type=int, default=4)
    args = parser.parse_args()

    _set_state(execution_provider=args.execution_provider)
    _load_runtime(args.execution_provider, args.cpu_threads)

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
