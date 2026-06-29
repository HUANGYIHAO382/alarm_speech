"""
TTS 播放调度器 — L1 片段拼合 + L2 整句复播
==========================================
MOSS 播报前的核心决策层：查缓存 → 拼合 / 补合成 / 整句回退。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from alarm_processor import RULE_AUTO, polish_speech_for_tts, speech_text_hash, split_speech_to_segments
from tts_cache_store import SOURCE_RUNTIME, STATUS_READY, get_cache_store
from tts_config import get_cache_settings, project_root
from tts_wav_stitch import stitch_wav_bytes

logger = logging.getLogger("tts_cache")


@dataclass
class PlaybackResult:
    """播放调度结果。"""

    mode: str  # utterance | stitch | full_moss | fallback
    wav_bytes: bytes
    summary: str
    stitch_hits: int = 0
    stitch_total: int = 0
    elapsed_ms: float = 0.0
    text: str = ""
    save_utterance: bool = False  # 首次成功后异步落盘 L2


class TtsPlaybackPlanner:
    """MOSS 播报路径规划。"""

    def __init__(
        self,
        synthesize_fn: Callable[[str], bytes],
        *,
        stitch_enabled: bool = True,
        cache_enabled: bool = True,
    ) -> None:
        self._synthesize = synthesize_fn
        self._stitch_enabled = stitch_enabled
        self._cache_enabled = cache_enabled
        self._store = get_cache_store()
        self._log_path = project_root() / "logs" / "tts_cache.log"

    def _log(self, msg: str) -> None:
        logger.info(msg)
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    def resolve(
        self,
        text: str,
        *,
        alarm_id: Any = None,
        rule_id: str = RULE_AUTO,
        is_repeat: bool = False,
        progress: Optional[Callable[[str, float, str], None]] = None,
    ) -> PlaybackResult:
        """
        决定如何获取待播放 WAV。
        :param is_repeat: 是否重复播报（优先 L2）
        """
        t0 = time.perf_counter()
        norm = polish_speech_for_tts(text)
        cfg = get_cache_settings()
        silence_ms = int(cfg.get("stitch_silence_ms", 80))

        if not self._cache_enabled or not cfg.get("enabled", True):
            wav = self._synthesize_full(norm, progress)
            elapsed = (time.perf_counter() - t0) * 1000
            return PlaybackResult(
                mode="full_moss",
                wav_bytes=wav,
                summary=f"整句 MOSS | {elapsed:.0f}ms",
                text=norm,
                elapsed_ms=elapsed,
            )

        text_hash = speech_text_hash(norm)

        # L2 整句复播（重复播路径）
        if is_repeat and alarm_id is not None:
            utter = self._store.get_utterance(alarm_id, text_hash)
            if utter:
                elapsed = (time.perf_counter() - t0) * 1000
                self._log(f"L2 命中 alarm={alarm_id} | {elapsed:.0f}ms")
                return PlaybackResult(
                    mode="utterance",
                    wav_bytes=utter,
                    summary=f"复播 整句缓存命中 | {elapsed:.0f}ms",
                    text=norm,
                    elapsed_ms=elapsed,
                )

        # 拼合关闭 → 整句 MOSS
        if not self._stitch_enabled or not cfg.get("stitch_enabled", True):
            wav = self._synthesize_full(norm, progress)
            elapsed = (time.perf_counter() - t0) * 1000
            result = PlaybackResult(
                mode="full_moss",
                wav_bytes=wav,
                summary=f"整句 MOSS（拼合已关闭）| {elapsed:.0f}ms",
                text=norm,
                elapsed_ms=elapsed,
                save_utterance=alarm_id is not None,
            )
            return result

        segments = split_speech_to_segments(norm, rule_id)
        if not segments:
            wav = self._synthesize_full(norm, progress)
            elapsed = (time.perf_counter() - t0) * 1000
            result = PlaybackResult(
                mode="full_moss",
                wav_bytes=wav,
                summary=f"整句 MOSS（不可拆槽）| {elapsed:.0f}ms",
                text=norm,
                elapsed_ms=elapsed,
                save_utterance=alarm_id is not None,
            )
            return result

        # L1 片段查缓存 + 补合成
        wav_parts: list[bytes] = []
        hits = 0
        total = len(segments)
        try:
            for seg in segments:
                cached = self._store.get_fragment(seg)
                if cached:
                    self._store.touch_hit(seg)
                    wav_parts.append(cached)
                    hits += 1
                else:
                    if progress:
                        progress("synthesize", 0.0, f"合成片段: {seg}")
                    wav = self._synthesize(seg)
                    self._store.put_fragment(seg, wav, source=SOURCE_RUNTIME)
                    self._store.upsert_hotword(seg, source=SOURCE_RUNTIME, status=STATUS_READY)
                    wav_parts.append(wav)

            stitched = stitch_wav_bytes(wav_parts, silence_ms=silence_ms)
            elapsed = (time.perf_counter() - t0) * 1000
            summary = f"拼合 {hits}/{total} · {elapsed:.0f}ms"
            self._log(f"{summary} | {norm}")
            result = PlaybackResult(
                mode="stitch",
                wav_bytes=stitched,
                summary=summary,
                stitch_hits=hits,
                stitch_total=total,
                text=norm,
                elapsed_ms=elapsed,
                save_utterance=alarm_id is not None,
            )
            return result
        except Exception as err:
            self._log(f"拼合失败，回退整句: {err}")
            wav = self._synthesize_full(norm, progress)
            elapsed = (time.perf_counter() - t0) * 1000
            return PlaybackResult(
                mode="fallback",
                wav_bytes=wav,
                summary=f"回退整句 MOSS | {elapsed:.0f}ms",
                text=norm,
                elapsed_ms=elapsed,
                save_utterance=alarm_id is not None,
            )

    def _synthesize_full(self, text: str, progress: Optional[Callable] = None) -> bytes:
        if progress:
            progress("synthesize", 0.0, "整句合成中…")
        return self._synthesize(text)

    def save_utterance_after_play(self, alarm_id: Any, result: PlaybackResult) -> None:
        """首次播放成功后写入 L2（若尚未写入）。"""
        if not result.save_utterance or alarm_id is None:
            return
        text_hash = speech_text_hash(result.text)
        if self._store.has_utterance(alarm_id, text_hash):
            return
        self._store.put_utterance(
            alarm_id,
            result.text,
            text_hash,
            result.wav_bytes,
            first_play_mode=result.mode,
        )
