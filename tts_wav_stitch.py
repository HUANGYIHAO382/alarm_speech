"""
WAV 拼合模块 — 纯标准库实现
============================
将多段 MOSS 合成的 WAV 按顺序拼接，片段间插入静音间隔。
"""

from __future__ import annotations

import io
import struct
import wave
from typing import Iterable


def _read_wav_pcm(wav_bytes: bytes) -> tuple[int, int, bytes]:
    """
    读取 WAV 字节，返回 (采样率, 声道数, PCM 数据)。
    仅支持 16-bit PCM。
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if sampwidth != 2:
        raise ValueError(f"仅支持 16-bit PCM，当前 sampwidth={sampwidth}")
    return rate, channels, frames


def _resample_linear(pcm: bytes, src_rate: int, dst_rate: int, channels: int) -> bytes:
    """简单线性重采样（采样率不一致时使用）。"""
    if src_rate == dst_rate:
        return pcm
    if channels != 1:
        raise ValueError("重采样暂仅支持单声道")

    n_samples = len(pcm) // 2
    if n_samples == 0:
        return pcm

    samples = struct.unpack(f"<{n_samples}h", pcm)
    ratio = dst_rate / src_rate
    out_len = max(1, int(n_samples * ratio))
    out: list[int] = []
    for i in range(out_len):
        src_pos = i / ratio
        idx = int(src_pos)
        frac = src_pos - idx
        if idx >= n_samples - 1:
            out.append(samples[-1])
        else:
            val = samples[idx] * (1 - frac) + samples[idx + 1] * frac
            out.append(int(max(-32768, min(32767, val))))
    return struct.pack(f"<{len(out)}h", *out)


def _make_silence_ms(rate: int, channels: int, ms: int) -> bytes:
    """生成指定毫秒数的静音 PCM。"""
    n_frames = int(rate * ms / 1000)
    return b"\x00\x00" * n_frames * channels


def _pcm_to_wav(pcm: bytes, rate: int, channels: int = 1) -> bytes:
    """将 PCM 数据封装为 WAV 字节。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def stitch_wav_bytes(
    segments: Iterable[bytes],
    silence_ms: int = 80,
    fade_ms: int = 10,
) -> bytes:
    """
    将多段 WAV 拼合为一条 WAV。
    :param segments: WAV 字节列表（按播放顺序）
    :param silence_ms: 片段间静音毫秒数
    :param fade_ms: 边界淡入淡出毫秒数（0 表示关闭）
    """
    seg_list = [s for s in segments if s]
    if not seg_list:
        raise ValueError("无有效 WAV 片段")
    if len(seg_list) == 1:
        return seg_list[0]

    # 以第一段采样率为基准
    base_rate, base_channels, _ = _read_wav_pcm(seg_list[0])
    pcm_parts: list[bytes] = []
    silence = _make_silence_ms(base_rate, base_channels, silence_ms) if silence_ms > 0 else b""

    for i, wav in enumerate(seg_list):
        rate, channels, pcm = _read_wav_pcm(wav)
        if channels != base_channels:
            raise ValueError("片段声道数不一致")
        if rate != base_rate:
            pcm = _resample_linear(pcm, rate, base_rate, channels)
        if fade_ms > 0 and len(pcm) >= 4:
            pcm = _apply_fade(pcm, base_rate, fade_ms)
        pcm_parts.append(pcm)
        if silence and i < len(seg_list) - 1:
            pcm_parts.append(silence)

    return _pcm_to_wav(b"".join(pcm_parts), base_rate, base_channels)


def _apply_fade(pcm: bytes, rate: int, fade_ms: int) -> bytes:
    """片段首尾线性淡入淡出，减少拼接爆音。"""
    n = len(pcm) // 2
    fade_samples = min(n // 4, int(rate * fade_ms / 1000))
    if fade_samples < 1:
        return pcm

    samples = list(struct.unpack(f"<{n}h", pcm))
    for i in range(fade_samples):
        factor = i / fade_samples
        samples[i] = int(samples[i] * factor)
        samples[-(i + 1)] = int(samples[-(i + 1)] * factor)
    return struct.pack(f"<{n}h", *samples)
