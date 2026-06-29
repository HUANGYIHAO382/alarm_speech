"""
语音播报模块 (TTS Engine)
============================
支持三种引擎 (界面下拉切换):
    - xfyun  : 科大讯飞 WebSocket API (需联网 + config.local.json 密钥)
    - moss   : MOSS-TTS-Nano 本地 ONNX (需 setup_moss.ps1 安装)
    - local  : Windows 离线 SAPI (win32com -> pyttsx3 -> PowerShell 兜底)
"""

from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from tts_config import (
    create_moss_client,
    create_xfyun_client,
    get_cache_settings,
    get_xfyun_settings,
    is_moss_configured,
    load_merged_config,
)

BACKEND_XFYUN = "xfyun"
BACKEND_MOSS = "moss"
BACKEND_LOCAL = "local"

BACKEND_LABELS = {
    BACKEND_XFYUN: "讯飞在线",
    BACKEND_MOSS: "MOSS 本地",
    BACKEND_LOCAL: "Windows SAPI 离线",
}


def is_xfyun_configured() -> bool:
    s = get_xfyun_settings(load_merged_config())
    return bool(s["app_id"] and s["api_key"] and s["api_secret"])


def default_backend_mode() -> str:
    if is_xfyun_configured():
        return BACKEND_XFYUN
    if is_moss_configured():
        return BACKEND_MOSS
    return BACKEND_LOCAL


class TTSEngine:
    def __init__(self, dedup_window_seconds: float = 3.0):
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._enabled = True
        self._dedup_window = dedup_window_seconds
        self._recent: dict[str, float] = {}
        self._lock = threading.Lock()
        self._mode_lock = threading.Lock()
        self._last_error: str = ""

        self._xfyun_client = None
        self._moss_client = None
        self._backend_mode = default_backend_mode()
        self._moss_provider_override: Optional[str] = None
        self._moss_cpu_threads_override: Optional[int] = None
        self._moss_prewarm_enabled = self._load_moss_prewarm_default()
        self._progress_callback: Optional[Callable[[str, float, str], None]] = None
        # 拼合/缓存摘要回调（供终端显示）
        self._cache_summary_callback: Optional[Callable[[str], None]] = None
        self._stitch_enabled = bool(get_cache_settings().get("stitch_enabled", True))
        self._planner = None
        # MOSS 单次任务计时（预热 / 合成），供进度条与总耗时展示
        self._moss_job_t0: Optional[float] = None
        self._moss_last_total_seconds: float = 0.0
        # SAPI Rate 范围 -10(最慢) ~ 10(最快), 默认 -3 略慢于系统默认
        self._local_sapi_rate = -4

        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # ---------- 对外接口 ----------
    def speak(self, text: str) -> None:
        """普通播报（无告警 ID，不走 L2）。"""
        self.speak_alarm(text, alarm_id=None, rule_id=None, is_repeat=False)

    def speak_alarm(
        self,
        text: str,
        *,
        alarm_id: Any = None,
        rule_id: Optional[str] = None,
        is_repeat: bool = False,
    ) -> None:
        """
        告警播报：MOSS 模式下走 TtsPlaybackPlanner（拼合/L2）。
        """
        if not text or not self._enabled:
            return

        text = self._prepare_text_for_tts(text)
        if not text:
            return

        # 去重 Key：同一文本在窗口内不重复入队
        dedup_key = f"{alarm_id}|{text}" if alarm_id is not None else text
        if self._dedup_window > 0:
            now = time.time()
            with self._lock:
                last = self._recent.get(dedup_key, 0)
                if now - last < self._dedup_window:
                    return
                self._recent[dedup_key] = now

        job = ("__alarm__", text, alarm_id, rule_id, is_repeat)
        self._queue.put(job)

    def set_cache_summary_callback(self, callback: Optional[Callable[[str], None]]) -> None:
        """设置拼合/缓存摘要回调，供 UI 终端显示。"""
        self._cache_summary_callback = callback

    def is_stitch_enabled(self) -> bool:
        return self._stitch_enabled

    def set_stitch_enabled(self, enabled: bool) -> None:
        """开启/关闭片段拼合。"""
        self._stitch_enabled = bool(enabled)
        self._planner = None

    def synthesize_moss_bytes(self, text: str) -> bytes:
        """供 WarmupJob 使用的 MOSS 合成（阻塞）。"""
        err = self._ensure_moss()
        if err:
            raise RuntimeError(err)
        if self._moss_client is None:
            raise RuntimeError("MOSS 客户端未初始化")
        return self._moss_client.synthesize_for_playback(
            self._prepare_text_for_tts(text),
            progress=self._emit_moss_progress,
        )

    def _get_planner(self):
        if self._planner is None:
            from tts_playback_planner import TtsPlaybackPlanner
            cfg = get_cache_settings()
            self._planner = TtsPlaybackPlanner(
                synthesize_fn=self.synthesize_moss_bytes,
                stitch_enabled=self._stitch_enabled,
                cache_enabled=cfg.get("enabled", True),
            )
        return self._planner

    def _init_warmup_job(self) -> None:
        """向 WarmupJob 注入 MOSS 合成函数。"""
        try:
            from tts_warmup_job import WarmupJob
            job = WarmupJob.instance()
            job.set_synthesize_fn(self.synthesize_moss_bytes)
        except ImportError:
            pass

    def speak_local(self, text: str) -> None:
        """
        强制 Windows SAPI 离线播报（兼容旧接口）。
        不受「语音引擎」下拉框影响，不受「语音播报」开关限制。
        """
        text = self._prepare_text_for_tts(text)
        if not text:
            return
        self._queue.put(("__force_local__", text))

    def speak_preview(self, text: str) -> None:
        """
        试听当前选中的语音引擎。
        不受「语音播报」开关限制，不做去重。
        """
        text = self._prepare_text_for_tts(text)
        if not text:
            return
        self._queue.put(text)

    def speak_preview_blocking(self, text: str) -> str:
        """
        试听并阻塞直到完成（用于 UI 进度条）。
        返回空字符串表示成功，否则为错误信息。
        """
        text = self._prepare_text_for_tts(text)
        if not text:
            return "内容为空"

        try:
            mode = self.get_backend()
            if mode == BACKEND_XFYUN:
                err = self._ensure_xfyun()
                if err:
                    return err
                self._speak_xfyun(text)
            elif mode == BACKEND_MOSS:
                err = self._ensure_moss()
                if err:
                    return err
                try:
                    self._speak_moss(text)
                finally:
                    self._maybe_release_moss_after_work()
            else:
                local_backend, speaker, engine = self._init_local_backend()
                self._speak_local(local_backend, speaker, engine, text)
            self._last_error = ""
            return ""
        except Exception as err:
            self._last_error = str(err)
            try:
                from app_logger import get_logger
                get_logger("tts").exception("试听失败: %s", err)
            except ImportError:
                pass
            return str(err)

    def set_progress_callback(
        self, callback: Optional[Callable[[str, float, str], None]]
    ) -> None:
        """设置 MOSS 合成进度回调: (阶段, 已用秒数, 说明)。"""
        self._progress_callback = callback

    def get_moss_last_elapsed(self) -> float:
        """上一次 MOSS 任务（预热或合成）的总耗时（秒）。"""
        return self._moss_last_total_seconds

    def _begin_moss_job(self) -> None:
        """标记 MOSS 任务开始，用于统计总耗时。"""
        if self._moss_job_t0 is None:
            self._moss_job_t0 = time.perf_counter()

    def _moss_job_elapsed(self) -> float:
        if self._moss_job_t0 is None:
            return 0.0
        return time.perf_counter() - self._moss_job_t0

    def _finish_moss_job(self) -> float:
        """结束计时并记录总耗时。"""
        total = self._moss_job_elapsed()
        if total > 0:
            self._moss_last_total_seconds = total
        self._moss_job_t0 = None
        return total

    def _emit_moss_progress(self, phase: str, step_elapsed: float, msg: str) -> None:
        """
        包装 MOSS 进度：统一附加「已用时 / 总耗时」。
        phase: daemon / loading / synthesize / done / ready / ...
        """
        cb = self._progress_callback
        if cb is None:
            return

        active_phases = ("daemon", "loading", "synthesize", "cli", "fallback")
        if phase in active_phases:
            self._begin_moss_job()

        total = self._moss_job_elapsed() if self._moss_job_t0 is not None else step_elapsed
        base_msg = (msg or "").strip()

        if phase == "done":
            total = self._finish_moss_job() or total
            display = "合成完成"
        elif phase == "ready":
            total = self._finish_moss_job() or total
            display = "预热完成" if total > 0 else (base_msg or "模型已预热就绪")
        else:
            display = base_msg or "处理中"

        cb(phase, total, display)

    def set_moss_execution_provider(self, provider: str) -> str:
        """
        切换 MOSS 运行设备 cpu / cuda。返回空字符串表示成功。
        会重启 MOSS 守护进程使新模式生效。
        """
        provider = (provider or "").strip().lower()
        if provider not in ("cpu", "cuda"):
            return f"未知 MOSS 设备模式: {provider}"

        self._moss_provider_override = provider
        if self._moss_client is not None:
            self._moss_client.shutdown_daemon()
            self._moss_client = None
        self._last_error = ""
        return ""

    def get_moss_execution_provider(self) -> str:
        if self._moss_provider_override:
            return self._moss_provider_override
        from tts_config import get_moss_settings
        return get_moss_settings().get("execution_provider", "cpu")

    def set_moss_cpu_threads(self, threads: int) -> str:
        """纯 CPU 模式下 ONNX 算子内并行线程数；变更后重启守护进程。"""
        threads = max(1, int(threads))
        self._moss_cpu_threads_override = threads
        if self._moss_client is not None:
            self._moss_client.shutdown_daemon()
            self._moss_client = None
        self._last_error = ""
        return ""

    def get_moss_cpu_threads(self) -> int:
        if self._moss_cpu_threads_override is not None:
            return self._moss_cpu_threads_override
        from tts_config import get_moss_settings
        return int(get_moss_settings().get("cpu_threads", 4))

    @staticmethod
    def _load_moss_prewarm_default() -> bool:
        """从 config.local.json 读取 MOSS 预热默认值。"""
        try:
            from tts_config import get_moss_settings
            return bool(get_moss_settings().get("prewarm", False))
        except Exception:
            return False

    def is_moss_prewarm_enabled(self) -> bool:
        """是否开启 MOSS 模型常驻预热。"""
        return self._moss_prewarm_enabled

    def set_moss_prewarm(self, enabled: bool) -> str:
        """
        切换 MOSS 预热模式。
        开启：模型常驻内存；关闭：立即释放守护进程。
        """
        self._moss_prewarm_enabled = bool(enabled)
        if not enabled:
            self.release_moss_memory()
        self._last_error = ""
        return ""

    def is_moss_daemon_ready(self) -> bool:
        """守护进程是否已加载模型（仅 MOSS 模式有意义）。"""
        if self._moss_client is None:
            return False
        return self._moss_client.is_daemon_alive()

    def warmup_moss(self) -> str:
        """
        阻塞式预热 MOSS 守护进程。
        返回空字符串表示成功，否则为错误信息。
        """
        err = self._ensure_moss()
        if err:
            return err
        if self._moss_client is None:
            return "MOSS 客户端未初始化"
        if self._moss_client.is_daemon_alive():
            self._emit_moss_progress("ready", 0.0, "模型已预热就绪")
            return ""
        self._begin_moss_job()
        try:
            self._moss_client.warmup_daemon(progress=self._emit_moss_progress)
            self._emit_moss_progress("ready", 0.0, "预热完成")
            return ""
        except Exception as err:
            self._moss_job_t0 = None
            self._last_error = str(err)
            return str(err)

    def release_moss_memory(self) -> None:
        """停止守护进程并释放模型占用的内存。"""
        if self._moss_client is not None:
            self._moss_client.shutdown_daemon()
            self._moss_client = None

    def _maybe_release_moss_after_work(self) -> None:
        """非预热模式下，播报结束后释放模型。"""
        if self._moss_prewarm_enabled:
            return
        if self.get_backend() != BACKEND_MOSS:
            return
        self.release_moss_memory()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def is_enabled(self) -> bool:
        return self._enabled

    def get_backend(self) -> str:
        with self._mode_lock:
            return self._backend_mode

    def backend_name(self) -> str:
        """兼容旧接口, 返回模式 key (xfyun / local)。"""
        return self.get_backend()

    def backend_label(self) -> str:
        return BACKEND_LABELS.get(self.get_backend(), self.get_backend())

    def set_backend(self, mode: str) -> str:
        """
        切换 TTS 引擎。返回空字符串表示成功, 否则为错误提示。
        """
        mode = (mode or "").strip().lower()
        if mode not in BACKEND_LABELS:
            return f"未知引擎: {mode}"

        if mode == BACKEND_XFYUN:
            err = self._ensure_xfyun()
            if err:
                return err

        if mode == BACKEND_MOSS:
            err = self._ensure_moss()
            if err:
                return err

        previous_mode = self.get_backend()
        with self._mode_lock:
            if previous_mode == BACKEND_MOSS and mode != BACKEND_MOSS:
                self.release_moss_memory()
            self._backend_mode = mode
        self._last_error = ""
        return ""

    def last_error(self) -> str:
        return self._last_error

    def get_local_rate(self) -> int:
        return self._local_sapi_rate

    def set_local_rate(self, rate: int) -> None:
        """设置 Windows SAPI 离线语速, 范围 -10 ~ 10。"""
        self._local_sapi_rate = max(-10, min(10, int(rate)))

    @staticmethod
    def _prepare_text_for_tts(text: str) -> str:
        """播报前统一润色，与 test_sapi_tts.py 样例效果一致。"""
        try:
            from alarm_processor import polish_speech_for_tts
            return polish_speech_for_tts(text.strip())
        except ImportError:
            return text.strip()

    def clear_queue(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    # ---------- 讯飞初始化 ----------
    def _ensure_xfyun(self) -> str:
        """确保讯飞客户端可用, 失败返回错误信息。"""
        if self._xfyun_client is not None:
            return ""
        if not is_xfyun_configured():
            return "讯飞未配置: 请在 config.local.json 填写 app_id / api_key / api_secret"
        try:
            self._xfyun_client = create_xfyun_client()
            return ""
        except Exception as err:
            return f"讯飞初始化失败: {err}"

    def _ensure_moss(self) -> str:
        """确保 MOSS 客户端可用, 失败返回错误信息。"""
        if self._moss_client is not None:
            self._init_warmup_job()
            return ""
        if not is_moss_configured():
            return (
                "MOSS 未安装: 请在项目根目录运行 setup_moss.ps1，"
                "或配置 config.local.json 中的 tts.moss"
            )
        try:
            from tts_config import create_moss_client

            self._moss_client = create_moss_client(
                execution_provider=self.get_moss_execution_provider(),
                cpu_threads=self.get_moss_cpu_threads(),
            )
            ok, err = self._moss_client.is_ready()
            if not ok:
                return err
            return ""
        except Exception as err:
            return f"MOSS 初始化失败: {err}"
        finally:
            self._init_warmup_job()

    # ---------- 内部: 后台消费 ----------
    def _run(self) -> None:
        local_backend, speaker, engine = self._init_local_backend()
        while True:
            item = self._queue.get()
            if not item:
                continue

            force_local = False
            alarm_id = None
            rule_id = None
            is_repeat = False

            if isinstance(item, tuple) and len(item) == 2 and item[0] == "__force_local__":
                force_local = True
                text = item[1]
            elif isinstance(item, tuple) and len(item) == 5 and item[0] == "__alarm__":
                _, text, alarm_id, rule_id, is_repeat = item
            else:
                text = item

            try:
                if force_local:
                    self._speak_local(local_backend, speaker, engine, text)
                else:
                    mode = self.get_backend()
                    if mode == BACKEND_XFYUN:
                        err = self._ensure_xfyun()
                        if err:
                            raise RuntimeError(err)
                        self._speak_xfyun(text)
                    elif mode == BACKEND_MOSS:
                        err = self._ensure_moss()
                        if err:
                            raise RuntimeError(err)
                        try:
                            self._speak_moss(
                                text,
                                alarm_id=alarm_id,
                                rule_id=rule_id,
                                is_repeat=is_repeat,
                            )
                        finally:
                            self._maybe_release_moss_after_work()
                    else:
                        self._speak_local(local_backend, speaker, engine, text)
                self._last_error = ""
            except Exception as err:
                self._last_error = str(err)
                try:
                    from app_logger import get_logger
                    get_logger("tts").exception("TTS 播报失败: %s", err)
                except ImportError:
                    pass

    def _speak_local(self, local_backend, speaker, engine, text: str) -> None:
        rate = self._local_sapi_rate
        if local_backend == "win32com":
            speaker.Rate = rate
            speaker.Speak(text)
        elif local_backend == "pyttsx3":
            engine.setProperty("rate", 200 + rate * 10)
            engine.say(text)
            engine.runAndWait()
        else:
            self._speak_via_powershell(text, rate)

    def _speak_xfyun(self, text: str) -> None:
        wav_bytes = self._xfyun_client.synthesize_for_playback(text)
        self._play_wav_bytes(wav_bytes)

    def _speak_moss(
        self,
        text: str,
        *,
        alarm_id: Any = None,
        rule_id: Optional[str] = None,
        is_repeat: bool = False,
    ) -> None:
        self._begin_moss_job()
        cfg = get_cache_settings()
        use_planner = cfg.get("enabled", True)

        if use_planner:
            planner = self._get_planner()
            result = planner.resolve(
                text,
                alarm_id=alarm_id,
                rule_id=rule_id or "auto",
                is_repeat=is_repeat,
                progress=self._emit_moss_progress,
            )
            self._play_wav_bytes(result.wav_bytes)
            self._emit_cache_summary(result.summary)
            if result.save_utterance and alarm_id is not None:
                planner.save_utterance_after_play(alarm_id, result)
        else:
            wav_bytes = self._moss_client.synthesize_for_playback(
                text,
                progress=self._emit_moss_progress,
            )
            self._play_wav_bytes(wav_bytes)

    def _emit_cache_summary(self, summary: str) -> None:
        if summary and self._cache_summary_callback:
            self._cache_summary_callback(summary)

    @staticmethod
    def _play_wav_bytes(wav_bytes: bytes) -> None:
        """将 WAV 字节写入临时文件并用 winsound 播放。"""
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            Path(path).write_bytes(wav_bytes)
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _select_chinese_sapi_voice(speaker) -> None:
        """优先选用 Windows 中文语音包 (如 Microsoft Huihui)。"""
        try:
            voices = speaker.GetVoices()
            keywords = ("huihui", "kangkang", "yaoyao", "chinese", "zh-cn", "0804", "中文")
            for i in range(voices.Count):
                desc = voices.Item(i).GetDescription().lower()
                if any(k in desc for k in keywords):
                    speaker.Voice = voices.Item(i)
                    return
        except Exception:
            pass

    @staticmethod
    def _select_chinese_pyttsx3_voice(engine) -> None:
        try:
            for voice in engine.getProperty("voices"):
                name = f"{voice.name} {voice.id}".lower()
                if any(k in name for k in ("chinese", "huihui", "zh-cn", "0804")):
                    engine.setProperty("voice", voice.id)
                    return
        except Exception:
            pass

    def _init_local_backend(self):
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
            pythoncom.CoInitialize()
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            self._select_chinese_sapi_voice(speaker)
            return "win32com", speaker, None
        except Exception:
            pass

        try:
            import pyttsx3  # type: ignore
            engine = pyttsx3.init()
            self._select_chinese_pyttsx3_voice(engine)
            engine.setProperty("rate", 160)
            return "pyttsx3", None, engine
        except Exception:
            pass

        return "powershell", None, None

    @staticmethod
    def _speak_via_powershell(text: str, rate: int = 0) -> None:
        ps_script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$voices = $s.GetInstalledVoices(); "
            "foreach ($v in $voices) { "
            "  $info = $v.VoiceInfo; "
            "  if ($info.Culture.Name -like 'zh*' -or $info.Name -match 'Huihui|Kangkang|Chinese') "
            "  { $s.SelectVoice($info.Name); break } "
            "} "
            f"$s.Rate = {rate}; "
            "$t = [Console]::In.ReadToEnd(); "
            "$s.Speak($t);"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            input=text,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


_default_engine: Optional[TTSEngine] = None


def get_default_engine() -> TTSEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = TTSEngine(dedup_window_seconds=3.0)
    return _default_engine


if __name__ == "__main__":
    eng = get_default_engine()
    print(f"TTS 引擎: {eng.backend_label()}")
    eng.speak("语音播报模块自检完成")
    time.sleep(8)
