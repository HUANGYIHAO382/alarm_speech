"""
MOSS-TTS-Nano 客户端（守护进程 + GPU/CPU 模式）
==============================================
优先连接常驻 moss_daemon.py，模型常驻内存，合成速度显著提升。
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

import requests

ProgressCallback = Callable[[str, float, str], None]

_DEFAULT_PORT = 18764
_DAEMON_START_TIMEOUT = 180.0


class MossTtsClient:
    """MOSS 语音合成客户端。"""

    _daemon_proc: Optional[subprocess.Popen] = None
    _daemon_provider: str = ""
    _daemon_port: int = _DEFAULT_PORT

    def __init__(
        self,
        python_exe: str,
        repo_dir: str,
        *,
        backend: str = "onnx",
        voice: str = "Junhao",
        prompt_audio_path: str = "",
        model_dir: str = "",
        cpu_threads: int = 4,
        execution_provider: str = "cpu",
        daemon_port: int = _DEFAULT_PORT,
    ) -> None:
        self._python = Path(python_exe).expanduser().resolve()
        if not self._python.is_file():
            raise ValueError(f"MOSS Python 不存在: {self._python}")

        self._repo_dir = Path(repo_dir).expanduser().resolve()
        if not (self._repo_dir / "infer_onnx.py").is_file():
            raise ValueError(f"MOSS 仓库不完整: {self._repo_dir}")

        self._project_root = Path(__file__).resolve().parent
        self._backend = (backend or "onnx").strip().lower()
        self._voice = (voice or "Junhao").strip()
        self._prompt_audio_path = (prompt_audio_path or "").strip()
        self._model_dir = (model_dir or "").strip()
        self._cpu_threads = max(1, int(cpu_threads))
        self._execution_provider = (execution_provider or "cpu").strip().lower()
        self._daemon_port = int(daemon_port)

    @property
    def execution_provider(self) -> str:
        return self._execution_provider

    def is_ready(self) -> tuple[bool, str]:
        cli = self._resolve_cli_executable()
        if cli is None:
            return False, "未找到 moss-tts-nano，请运行 setup_moss.ps1"
        return True, ""

    def synthesize_for_playback(
        self,
        text: str,
        progress: Optional[ProgressCallback] = None,
    ) -> bytes:
        """合成 WAV 字节；优先走守护进程。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("合成内容不能为空")

        ok, err = self.is_ready()
        if not ok:
            raise RuntimeError(err)

        # 守护进程模式（推荐）
        try:
            return self._synthesize_via_daemon(text, progress=progress)
        except Exception as daemon_err:
            if progress:
                progress("fallback", 0.0, "守护进程不可用，回退 CLI 模式")
            # 回退一次性 CLI（较慢）
            return self._synthesize_via_cli(text, progress=progress, fallback_reason=str(daemon_err))

    def shutdown_daemon(self) -> None:
        """停止守护进程（切换 CPU/GPU 模式时调用）。"""
        proc = MossTtsClient._daemon_proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        MossTtsClient._daemon_proc = None
        MossTtsClient._daemon_provider = ""

    def _base_url(self) -> str:
        return f"http://127.0.0.1:{self._daemon_port}"

    def _synthesize_via_daemon(
        self,
        text: str,
        progress: Optional[ProgressCallback] = None,
    ) -> bytes:
        self._ensure_daemon(progress=progress)
        if progress:
            progress("synthesize", 0.0, "提交合成任务...")

        t0 = time.perf_counter()
        resp = requests.post(
            f"{self._base_url()}/synthesize",
            json={"text": text, "voice": self._voice},
            timeout=300,
        )
        elapsed = time.perf_counter() - t0

        if resp.status_code != 200:
            raise RuntimeError(f"守护进程合成失败 HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "合成失败")

        if progress:
            ms = data.get("elapsed_ms", int(elapsed * 1000))
            progress("done", elapsed, f"完成 {ms} ms")

        return base64.b64decode(data["wav_b64"])

    def _daemon_log_path(self) -> Path:
        return self._project_root / "logs" / "moss_daemon.log"

    def _read_daemon_log_tail(self, max_lines: int = 12) -> str:
        path = self._daemon_log_path()
        if not path.is_file():
            return "（无守护进程日志）"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-max_lines:]) if lines else "（日志为空）"
        except Exception as err:
            return f"读取日志失败: {err}"

    def _ensure_daemon(
        self,
        progress: Optional[ProgressCallback] = None,
        *,
        allow_cuda_fallback: bool = True,
    ) -> None:
        provider = self._execution_provider
        if (
            MossTtsClient._daemon_proc is not None
            and MossTtsClient._daemon_proc.poll() is None
            and MossTtsClient._daemon_provider == provider
        ):
            try:
                r = requests.get(f"{self._base_url()}/health", timeout=2)
                if r.json().get("ok"):
                    return
            except requests.RequestException:
                pass

        self.shutdown_daemon()
        if progress:
            progress("daemon", 0.0, f"启动守护进程 ({provider.upper()})...")

        daemon_script = self._project_root / "moss_daemon.py"
        cmd = [
            str(self._python),
            str(daemon_script),
            "--execution-provider",
            provider,
            "--port",
            str(self._daemon_port),
            "--cpu-threads",
            str(self._cpu_threads),
        ]

        log_path = self._daemon_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        log_file.write(
            f"\n--- daemon start {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"provider={provider} port={self._daemon_port} ---\n"
        )
        log_file.flush()

        from moss_cuda_env import build_subprocess_env

        MossTtsClient._daemon_proc = subprocess.Popen(
            cmd,
            cwd=str(self._repo_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=build_subprocess_env(self._python),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        MossTtsClient._daemon_provider = provider

        start_ts = time.time()
        deadline = start_ts + _DAEMON_START_TIMEOUT
        while time.time() < deadline:
            proc = MossTtsClient._daemon_proc
            if proc is not None and proc.poll() is not None:
                detail = self._read_daemon_log_tail()
                if provider == "cuda" and allow_cuda_fallback:
                    if progress:
                        progress(
                            "fallback",
                            0.0,
                            "GPU 加载失败，自动回退 CPU...",
                        )
                    self._execution_provider = "cpu"
                    self.shutdown_daemon()
                    return self._ensure_daemon(
                        progress=progress,
                        allow_cuda_fallback=False,
                    )
                raise RuntimeError(
                    f"MOSS 守护进程异常退出 (code={proc.returncode})。\n{detail}"
                )

            try:
                st = requests.get(f"{self._base_url()}/status", timeout=2).json()
                phase = st.get("phase", "")
                msg = st.get("message", "")
                if st.get("phase") == "error":
                    if provider == "cuda" and allow_cuda_fallback:
                        if progress:
                            progress("fallback", 0.0, f"GPU 失败: {msg}，回退 CPU...")
                        self._execution_provider = "cpu"
                        self.shutdown_daemon()
                        return self._ensure_daemon(
                            progress=progress,
                            allow_cuda_fallback=False,
                        )
                    raise RuntimeError(msg or "守护进程加载模型失败")
                if progress:
                    elapsed = time.time() - start_ts
                    progress(phase or "loading", elapsed, msg or "等待模型加载...")
                if st.get("ready"):
                    return
            except requests.RequestException:
                if progress:
                    progress("loading", time.time() - start_ts, "等待模型加载...")
            time.sleep(0.4)

        raise RuntimeError(
            "MOSS 守护进程启动超时，请查看 logs/moss_daemon.log 与 logs/app.log"
        )

    def _synthesize_via_cli(
        self,
        text: str,
        progress: Optional[ProgressCallback] = None,
        fallback_reason: str = "",
    ) -> bytes:
        if progress:
            progress("cli", 0.0, fallback_reason or "CLI 冷启动合成...")

        fd, wav_path = tempfile.mkstemp(suffix=".moss.wav")
        os.close(fd)
        Path(wav_path).unlink(missing_ok=True)

        try:
            t0 = time.perf_counter()
            self._run_generate(text, wav_path)
            if progress:
                progress("done", time.perf_counter() - t0, "CLI 合成完成")
            data = Path(wav_path).read_bytes()
            if not data:
                raise RuntimeError("MOSS 未生成音频")
            return data
        finally:
            Path(wav_path).unlink(missing_ok=True)

    def _resolve_cli_executable(self) -> Optional[Path]:
        scripts_dir = self._python.parent
        for name in ("moss-tts-nano.exe", "moss-tts-nano"):
            candidate = scripts_dir / name
            if candidate.is_file():
                return candidate
        return None

    def _run_generate(self, text: str, output_path: str) -> None:
        cli = self._resolve_cli_executable()
        if cli is None:
            raise RuntimeError("moss-tts-nano CLI 不可用")

        cmd = [
            str(cli), "generate", "--backend", self._backend,
            "--text", text, "--output", output_path,
            "--voice", self._voice,
            "--cpu-threads", str(self._cpu_threads),
            "--execution-provider", self._execution_provider,
            "--disable-wetext-processing",
        ]
        if self._model_dir:
            cmd.extend(["--onnx-model-dir", self._model_dir])
        if self._prompt_audio_path:
            prompt = Path(self._prompt_audio_path).expanduser().resolve()
            if prompt.is_file():
                cmd.extend(["--prompt-speech", str(prompt)])

        result = subprocess.run(
            cmd,
            cwd=str(self._repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"MOSS CLI 失败 (code={result.returncode}): {detail or '无输出'}")


if __name__ == "__main__":
    from tts_config import create_moss_client, is_moss_configured

    if not is_moss_configured():
        print("MOSS 未就绪")
        sys.exit(1)
    sample = sys.argv[1] if len(sys.argv) > 1 else "你好，MOSS 测试"
    client = create_moss_client()
    wav = client.synthesize_for_playback(sample, progress=lambda p, e, m: print(p, f"{e:.1f}s", m))
    print("OK bytes:", len(wav))
