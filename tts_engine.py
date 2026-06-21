"""
语音播报模块 (TTS Engine)
============================
支持两种引擎 (界面下拉切换):
    - xfyun  : 科大讯飞 WebSocket API (需联网 + config.local.json 密钥)
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
from typing import Optional

from tts_config import create_xfyun_client, get_xfyun_settings, load_merged_config

BACKEND_XFYUN = "xfyun"
BACKEND_LOCAL = "local"

BACKEND_LABELS = {
    BACKEND_XFYUN: "讯飞在线",
    BACKEND_LOCAL: "Windows SAPI 离线",
}


def is_xfyun_configured() -> bool:
    s = get_xfyun_settings(load_merged_config())
    return bool(s["app_id"] and s["api_key"] and s["api_secret"])


def default_backend_mode() -> str:
    return BACKEND_XFYUN if is_xfyun_configured() else BACKEND_LOCAL


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
        self._backend_mode = default_backend_mode()
        # SAPI Rate 范围 -10(最慢) ~ 10(最快), 默认 -3 略慢于系统默认
        self._local_sapi_rate = -4

        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # ---------- 对外接口 ----------
    def speak(self, text: str) -> None:
        if not text or not self._enabled:
            return

        text = self._prepare_text_for_tts(text)
        if not text:
            return

        if self._dedup_window > 0:
            now = time.time()
            with self._lock:
                last = self._recent.get(text, 0)
                if now - last < self._dedup_window:
                    return
                self._recent[text] = now

        self._queue.put(text)

    def speak_local(self, text: str) -> None:
        """
        强制 Windows SAPI 离线播报（UI 试听专用）。
        不受「语音引擎」下拉框影响，不受「语音播报」开关限制。
        """
        text = self._prepare_text_for_tts(text)
        if not text:
            return
        self._queue.put(("__force_local__", text))

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

        with self._mode_lock:
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

    # ---------- 内部: 后台消费 ----------
    def _run(self) -> None:
        local_backend, speaker, engine = self._init_local_backend()
        while True:
            item = self._queue.get()
            if not item:
                continue

            force_local = False
            if isinstance(item, tuple) and len(item) == 2 and item[0] == "__force_local__":
                force_local = True
                text = item[1]
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
                    else:
                        self._speak_local(local_backend, speaker, engine, text)
                self._last_error = ""
            except Exception as err:
                self._last_error = str(err)

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
