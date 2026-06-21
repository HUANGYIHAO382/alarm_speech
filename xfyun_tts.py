"""
科大讯飞在线语音合成（流式 WebSocket API v2）。
参考项目: B:\\text_to_speech\\core\\xfyun_tts.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import threading
from datetime import datetime
from time import mktime
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse
from wsgiref.handlers import format_date_time

DEFAULT_HOST_URL = "wss://tts-api.xfyun.cn/v2/tts"


class XfyunTtsClient:
    """科大讯飞流式语音合成客户端。"""

    def __init__(
        self,
        app_id: str,
        api_key: str,
        api_secret: str,
        host_url: str = DEFAULT_HOST_URL,
        vcn: str = "xiaoyan",
        auf: str = "audio/L16;rate=16000",
        speed: int = 50,
        volume: int = 50,
        pitch: int = 50,
        tte: str = "UTF8",
        sample_rate: int = 16000,
    ) -> None:
        if not all([app_id, api_key, api_secret]):
            raise ValueError(
                "讯飞 TTS 密钥不完整。请在 config.local.json 的 tts.xfyun 中填写 "
                "app_id、api_key、api_secret。"
            )
        self._app_id = app_id.strip()
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()
        self._host_url = host_url.strip()
        self._vcn = vcn
        self._auf = auf
        self._speed = _clamp_0_100(speed)
        self._volume = _clamp_0_100(volume)
        self._pitch = _clamp_0_100(pitch)
        self._tte = tte
        self._sample_rate = sample_rate

    def synthesize_for_playback(
        self,
        text: str,
        cancel_event: Optional[threading.Event] = None,
    ) -> bytes:
        """合成 WAV 字节流, 供 winsound 直接播放。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("合成内容不能为空")

        business: Dict[str, Any] = {
            "aue": "raw",
            "vcn": self._vcn,
            "speed": self._speed,
            "volume": self._volume,
            "pitch": self._pitch,
            "tte": self._tte,
            "auf": self._auf,
        }
        pcm = self._request_audio(text, business, cancel_event)
        return pcm_to_wav(pcm, sample_rate=self._sample_rate)

    def _request_audio(
        self,
        text: str,
        business: Dict[str, Any],
        cancel_event: Optional[threading.Event],
    ) -> bytes:
        import websocket

        url = build_auth_url(self._host_url, self._api_key, self._api_secret)
        text_b64 = base64.b64encode(text.encode("utf-8")).decode("utf-8")
        payload = {
            "common": {"app_id": self._app_id},
            "business": business,
            "data": {"status": 2, "text": text_b64},
        }

        audio_buffer = bytearray()
        ws = websocket.create_connection(url, timeout=60)
        try:
            ws.send(json.dumps(payload, ensure_ascii=False))
            while True:
                if cancel_event and cancel_event.is_set():
                    ws.close()
                    raise InterruptedError("语音合成已取消")

                raw = ws.recv()
                if not raw:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")

                msg = json.loads(raw)
                code = msg.get("code", -1)
                if code != 0:
                    raise RuntimeError(
                        f"讯飞 TTS 错误 code={code}: {msg.get('message', msg)}"
                    )

                data = msg.get("data") or {}
                audio_b64 = data.get("audio")
                if audio_b64:
                    audio_buffer.extend(base64.b64decode(audio_b64))
                if data.get("status") == 2:
                    break
        finally:
            try:
                ws.close()
            except Exception:
                pass

        if not audio_buffer:
            raise RuntimeError("讯飞 TTS 未返回音频数据")
        return bytes(audio_buffer)


def build_auth_url(host_url: str, api_key: str, api_secret: str) -> str:
    parsed = urlparse(host_url)
    host = parsed.netloc
    path = parsed.path or "/v2/tts"

    now = datetime.utcnow()
    date = format_date_time(mktime(now.timetuple()))

    signature_origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
    signature_sha = hmac.new(
        api_secret.encode("utf-8"),
        signature_origin.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    signature = base64.b64encode(signature_sha).decode("utf-8")

    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode(
        "utf-8"
    )
    query = urlencode({"authorization": authorization, "date": date, "host": host})
    return f"{parsed.scheme}://{host}{path}?{query}"


def pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    sample_width = 2
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    data_size = len(pcm_data)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        sample_width * 8,
        b"data",
        data_size,
    )
    return header + pcm_data


def _clamp_0_100(value: int) -> int:
    return max(0, min(100, int(value)))
