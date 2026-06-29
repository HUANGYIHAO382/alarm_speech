"""
历史告警热词分析 — 从历史记录提取高频片段
==========================================
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from alarm_processor import RULE_AUTO, alarm_to_speech, split_speech_to_segments
from tts_cache_store import SOURCE_HISTORY, STATUS_READY, get_cache_store
from tts_normalize import normalize_for_cache


@dataclass
class AnalysisResult:
    """历史分析结果。"""

    total_alarms: int = 0
    unique_segments: int = 0
    new_entries: list[dict] = field(default_factory=list)
    preview_by_type: dict[str, int] = field(default_factory=dict)
    preview_samples: list[str] = field(default_factory=list)
    estimated_mb: float = 0.0
    estimated_minutes: float = 0.0


def _slot_type_for_segment(seg: str, index: int, total: int) -> str:
    if seg in ("端口",):
        return "keyword_port"
    if seg in ("联通", "中断"):
        return "state"
    if seg.endswith("柜"):
        return "cabinet"
    if total == 4 and index == 1:
        return "device"
    if seg in ("告警", "恢复", "已恢复正常"):
        return "event"
    if index == 1 and total >= 2:
        return "device"
    return "phrase"


class HistoryHotwordAnalyzer:
    """从历史告警列表分析热词。"""

    def __init__(
        self,
        top_cabinets: int = 50,
        top_devices: int = 200,
    ) -> None:
        self.top_cabinets = top_cabinets
        self.top_devices = top_devices

    def analyze_alarms(
        self,
        alarms: list[dict],
        rule_id: str = RULE_AUTO,
        *,
        skip_existing_ready: bool = True,
    ) -> AnalysisResult:
        store = get_cache_store()
        existing_ready = set()
        if skip_existing_ready:
            for e in store.list_hotwords("all"):
                if e.get("status") == STATUS_READY:
                    existing_ready.add(normalize_for_cache(e.get("text", "")))

        freq: Counter[str] = Counter()
        slot_types: dict[str, str] = {}

        for alarm in alarms:
            speech = alarm_to_speech(alarm, rule_id)
            if not speech:
                continue
            segments = split_speech_to_segments(speech, rule_id)
            if not segments:
                # 整句不可拆时，把整句作为 phrase 统计（低优先级）
                norm = normalize_for_cache(speech)
                freq[norm] += 1
                slot_types[norm] = "phrase"
                continue
            for i, seg in enumerate(segments):
                norm = normalize_for_cache(seg)
                freq[norm] += 1
                slot_types[norm] = _slot_type_for_segment(norm, i, len(segments))

        cabinets = [(t, c) for t, c in freq.items() if slot_types.get(t) == "cabinet"]
        devices = [(t, c) for t, c in freq.items() if slot_types.get(t) == "device"]
        others = [
            (t, c) for t, c in freq.items()
            if slot_types.get(t) not in ("cabinet", "device")
            and t not in ("端口", "联通", "中断", "告警", "恢复", "已恢复正常")
        ]

        cabinets.sort(key=lambda x: -x[1])
        devices.sort(key=lambda x: -x[1])
        others.sort(key=lambda x: -x[1])

        selected: dict[str, int] = {}
        for t, c in cabinets[: self.top_cabinets]:
            selected[t] = c
        for t, c in devices[: self.top_devices]:
            selected[t] = c

        new_entries: list[dict] = []
        preview_by_type: dict[str, int] = {"cabinet": 0, "device": 0, "other": 0}

        for text, count in selected.items():
            if text in existing_ready:
                continue
            st = slot_types.get(text, "phrase")
            if st == "cabinet":
                preview_by_type["cabinet"] += 1
            elif st == "device":
                preview_by_type["device"] += 1
            else:
                preview_by_type["other"] += 1
            new_entries.append({
                "text": text,
                "source": SOURCE_HISTORY,
                "slot_type": st,
                "history_count": count,
            })

        samples = [e["text"] for e in new_entries[:8]]
        est_kb = sum(max(20, len(e["text"]) * 8) for e in new_entries)
        est_mb = round(est_kb / 1024, 2)
        # CPU 约 6–10s/条
        est_min = round(len(new_entries) * 8 / 60, 1)

        return AnalysisResult(
            total_alarms=len(alarms),
            unique_segments=len(freq),
            new_entries=new_entries,
            preview_by_type=preview_by_type,
            preview_samples=samples,
            estimated_mb=est_mb,
            estimated_minutes=est_min,
        )
