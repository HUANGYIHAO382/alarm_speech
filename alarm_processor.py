"""
告警信息处理模块 (Alarm Processor)
=====================================
职责：
    - 清洗服务端的脏数据 (HTML <br>、转义、字段缺失)
    - 终端展示用的格式化文本
    - 状态追踪 (Diffing): 维护本地缓存池, 对比新旧告警, 产出 4 种事件
    - 播报规则: 通用 / 端口表 (last(2) 解析) / 自动匹配
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, List, Optional

# ---------- 播报规则 ID (界面下拉框 value) ----------
RULE_AUTO = "auto"
RULE_GENERIC = "generic"
RULE_PORT_TABLE = "port_table"

SPEECH_RULE_OPTIONS = [
    (RULE_AUTO, "自动匹配"),
    (RULE_PORT_TABLE, "端口表告警"),
    (RULE_GENERIC, "通用规则"),
]


# ==========================================
# 数据清洗与展示格式
# ==========================================
def clean_html_content(raw_content: Optional[str]) -> str:
    """把 <br> / 转义符替换为换行, 并加 6 空格缩进, 形成可读层级文本。"""
    if not raw_content:
        return "      无详细内容"
    text = raw_content.replace("<br>", "\n").replace("\u003Cbr\u003E", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    return "\n".join(f"      {ln}" for ln in lines)


def raw_alarm_text(alarm: dict) -> str:
    """告警正文字符串 (去 HTML 换行, 便于正则)。"""
    content = alarm.get("alarm_content") or ""
    return content.replace("<br>", "\n").replace("\u003Cbr\u003E", "\n")


def format_alarm_for_console(alarm: dict, idx: Optional[int] = None) -> str:
    time_str = alarm.get("last_alarm_time", "")
    ip = alarm.get("ip", "未知IP")
    device = alarm.get("instance_name", "未知设备")
    title = alarm.get("alarm_title", "未命名告警")
    content = clean_html_content(alarm.get("alarm_content"))

    is_recover = alarm.get("is_recover", 0)
    status_icon = "🟢 [已恢复]" if is_recover == 1 else "🔴 [未处理]"

    head = f"{idx}. " if idx is not None else ""
    return (
        f"{head}{status_icon} {time_str}\n"
        f"   ➤ 节点: {device} ({ip})\n"
        f"   ➤ 事件: {title}\n"
        f"   ➤ 详情: \n{content}\n"
    )


def format_alarm_for_active_console(alarm: dict) -> str:
    content = clean_html_content(alarm.get("alarm_content"))
    return (
        f"🔴 {alarm.get('last_alarm_time')} | {alarm.get('ip')}\n"
        f"   ➤ {alarm.get('alarm_title')}\n"
        f"   ➤ 详情: \n{content}\n"
    )


# ==========================================
# 播报规则: 解析与转换
# ==========================================
# last(2) 英文状态 → TTS 中文（避免 SAPI 把 up/down 读成含糊英文）
PORT_STATE_TTS_CN = {
    "up": "联通",
    "down": "中断",
}


def format_device_for_tts(instance_name: Optional[str]) -> str:
    """
    设备名 → SAPI 友好格式。
    B05_NE20E-1 → B05柜，NE20E杠1
    """
    name = (instance_name or "").strip() or "未知设备"
    m = re.match(r"^([A-Za-z]\d+)_(.+)$", name)
    if m:
        cabinet, dev = m.group(1), m.group(2)
        dev = re.sub(r"([A-Za-z0-9]+)-([A-Za-z0-9]+)", r"\1杠\2", dev)
        return f"{cabinet}柜，{dev}"
    return re.sub(r"([A-Za-z0-9]+)-([A-Za-z0-9]+)", r"\1杠\2", name)


def build_port_table_speech_text(instance_name: Optional[str], state: str) -> str:
    """
    标准端口表播报（与 test_sapi_tts.py 样例一致）:
    B05柜，NE20E杠1，端口，联通 / 中断
    """
    device = format_device_for_tts(instance_name)
    state_cn = PORT_STATE_TTS_CN.get((state or "").lower(), state)
    return f"{device}，端口，{state_cn}"


# 简短别名，供测试脚本 / 外部调用
build_port_table_speech = build_port_table_speech_text


def polish_speech_for_tts(text: str) -> str:
    """
    兜底润色：委托 tts_normalize.normalize_for_cache，保证与缓存 Key 一致。
    """
    if not text:
        return text
    try:
        from tts_normalize import normalize_for_cache
        return normalize_for_cache(text)
    except ImportError:
        # 无 normalize 模块时的最小兜底
        out = text.strip()
        out = out.replace("柜的", "柜，")
        out = re.sub(r"([A-Za-z0-9]+)-([A-Za-z0-9]+)", r"\1杠\2", out)
        return out


def speech_text_hash(text: str) -> str:
    """整句文本 hash，供 L2 utterance 缓存 Key 使用。"""
    norm = polish_speech_for_tts(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def split_speech_to_segments(text: str, rule_id: str = RULE_AUTO) -> Optional[List[str]]:
    """
    将播报文本拆为片段列表，供 L1 拼合使用。
    端口表：B05柜，NE20E杠1，端口，联通 → 4 段
    通用/恢复：告警，设备，标题 → 3 段
    无法识别模板时返回 None（走整句 MOSS）。
    """
    if not text:
        return None

    norm = polish_speech_for_tts(text)
    rule_id = (rule_id or RULE_AUTO).strip().lower()

    # 端口表标准句式（4 段）
    port_m = re.match(r"^(.+柜)，(.+)，端口，(联通|中断)$", norm)
    if port_m:
        return [port_m.group(1), port_m.group(2), "端口", port_m.group(3)]

    if rule_id == RULE_PORT_TABLE:
        return None

    # 按中文逗号拆分，处理「告警/恢复 + 机柜设备 + 标题/状态」
    parts = [p for p in norm.split("，") if p]
    if len(parts) >= 3 and parts[0] in ("告警", "恢复"):
        if parts[0] == "恢复" and parts[-1] == "已恢复正常":
            if len(parts) == 3:
                return parts
            # 恢复，B05柜，NE20E杠1，已恢复正常 → 4 段
            if len(parts) == 4:
                return parts
        if parts[0] == "告警":
            if len(parts) == 3:
                return parts
            if len(parts) >= 4:
                # 告警，B05柜，NE20E杠1，标题…
                return [parts[0], parts[1], parts[2], "，".join(parts[3:])]

    # 恢复：恢复，{设备}，已恢复正常（设备名内含逗号时整段作为第 2 段）
    rec_m = re.match(r"^恢复，(.+)，已恢复正常$", norm)
    if rec_m:
        return ["恢复", rec_m.group(1), "已恢复正常"]

    # 通用告警：告警，{设备}，{标题}
    gen_m = re.match(r"^告警，(.+)，(.+)$", norm)
    if gen_m:
        return ["告警", gen_m.group(1), gen_m.group(2)]

    return None


def parse_last2_state(alarm: dict) -> Optional[str]:
    """
    从告警详情提取 last(2) 的值 (up / down)。
    规则: last(2) 是啥就播啥, 不看 last(1)。
    """
    text = raw_alarm_text(alarm)
    m = re.search(r"last\s*\(\s*2\s*\)\s*=\s*(up|down)", text, re.IGNORECASE)
    return m.group(1).lower() if m else None


def format_device_speech_name(instance_name: Optional[str]) -> str:
    """终端展示用设备名（保留原始样式）。"""
    name = (instance_name or "").strip()
    if not name:
        return "未知设备"
    m = re.match(r"^([A-Za-z]\d+)_(.+)$", name)
    if m:
        return f"{m.group(1)}柜的{m.group(2)}"
    return name


def is_port_table_alarm(alarm: dict) -> bool:
    title = alarm.get("alarm_title") or ""
    content = raw_alarm_text(alarm)
    return "端口表" in title or "端口表/状态" in content


def port_table_to_speech(alarm: dict) -> Optional[str]:
    """端口表告警 → B05柜，NE20E杠1，端口，联通/中断"""
    if not is_port_table_alarm(alarm):
        return None
    state = parse_last2_state(alarm)
    if not state:
        return None
    return build_port_table_speech_text(alarm.get("instance_name"), state)


def generic_to_speech(alarm: dict, *, recovered: bool = False) -> str:
    # 端口表类告警即使走通用规则，也优先用标准端口话术
    if is_port_table_alarm(alarm):
        pt = port_table_to_speech(alarm)
        if pt:
            return pt
    device = format_device_for_tts(alarm.get("instance_name", "未知设备"))
    title = alarm.get("alarm_title", "未命名告警")
    if recovered or alarm.get("is_recover", 0) == 1:
        return f"恢复，{device}，已恢复正常"
    return f"告警，{device}，{title}"


def _finalize_speech(text: str) -> str:
    return polish_speech_for_tts(text)


def alarm_to_speech(alarm: dict, rule_id: str = RULE_AUTO) -> str:
    """
    把单条告警 dict 转成播报文本。
    :param rule_id: auto / port_table / generic
    """
    rule_id = (rule_id or RULE_AUTO).strip().lower()

    if rule_id == RULE_GENERIC:
        return _finalize_speech(generic_to_speech(alarm))

    if rule_id == RULE_PORT_TABLE:
        text = port_table_to_speech(alarm)
        return text if text else _finalize_speech(generic_to_speech(alarm))

    # auto: 端口表能匹配则用端口表, 否则通用
    text = port_table_to_speech(alarm)
    if text:
        return text
    return _finalize_speech(generic_to_speech(alarm))


def speech_rule_label(rule_id: str) -> str:
    for rid, label in SPEECH_RULE_OPTIONS:
        if rid == rule_id:
            return label
    return rule_id


# ==========================================
# 状态追踪 (Diffing)
# ==========================================
@dataclass
class AlarmEvent:
    kind: str           # NEW / RECOVERED / DISMISSED / STALE
    alarm_id: object
    device: str
    ip: str
    title: str
    timestamp: str      # HH:MM:SS
    alarm: dict = field(default_factory=dict)  # 用于播报的完整告警 dict

    @property
    def log_line(self) -> str:
        icon = {
            "NEW": "🆕",
            "RECOVERED": "✅",
            "DISMISSED": "🔕",
            "STALE": "⚠️",
        }.get(self.kind, "•")
        verb = {
            "NEW": "新增告警",
            "RECOVERED": "设备恢复",
            "DISMISSED": "告警解除追踪(非恢复)",
            "STALE": "反查超时, 暂保留追踪",
        }.get(self.kind, self.kind)
        return f"{icon} [{self.timestamp}] {verb} | {self.device}({self.ip}) | {self.title}"


def event_to_speech(event: AlarmEvent, rule_id: str = RULE_AUTO) -> Optional[str]:
    """
    把 Diff 事件翻译成播报文本; 返回 None 表示不播报。
    RECOVERED 使用反查后的 alarm dict (含最新 last(2))。
    """
    if event.kind not in ("NEW", "RECOVERED"):
        return None

    alarm = event.alarm or {}
    rule_id = (rule_id or RULE_AUTO).strip().lower()

    recovered = event.kind == "RECOVERED"

    if rule_id == RULE_GENERIC:
        return _finalize_speech(generic_to_speech(alarm, recovered=recovered))

    if rule_id == RULE_PORT_TABLE:
        text = port_table_to_speech(alarm)
        if text:
            return text
        return _finalize_speech(generic_to_speech(alarm, recovered=recovered))

    # auto
    text = port_table_to_speech(alarm)
    if text:
        return text
    return _finalize_speech(generic_to_speech(alarm, recovered=recovered))


class AlarmTracker:
    """
    维护本地缓存池 known_alarms: { id: 告警dict }
    每轮拉取后调用 .diff(active_list, query_by_id) 返回事件列表。
    """

    def __init__(self, announce_existing: bool = False):
        self._known: dict = {}
        self._primed = False
        self._announce_existing = announce_existing

    def tracking_count(self) -> int:
        return len(self._known)

    def known_ids(self) -> Iterable:
        return self._known.keys()

    def diff(
        self,
        active_list: list,
        query_by_id: Callable[[object], Optional[dict]],
    ) -> list[AlarmEvent]:
        ts = datetime.now().strftime("%H:%M:%S")
        events: list[AlarmEvent] = []

        current_map = {}
        for a in active_list:
            aid = a.get("id")
            if aid is not None:
                current_map[aid] = a
        current_ids = set(current_map.keys())

        if not self._primed:
            self._known.update(current_map)
            self._primed = True
            if self._announce_existing:
                for aid, alarm in current_map.items():
                    events.append(self._make_event("NEW", aid, alarm, ts))
            return events

        for aid, alarm in current_map.items():
            if aid not in self._known:
                self._known[aid] = alarm
                events.append(self._make_event("NEW", aid, alarm, ts))

        for aid in list(self._known.keys()):
            if aid in current_ids:
                self._known[aid] = current_map[aid]
                continue

            detail = query_by_id(aid)
            cached = self._known[aid]

            if detail is None:
                events.append(self._make_event("STALE", aid, cached, ts))
                continue

            if detail.get("is_recover", 0) == 1:
                del self._known[aid]
                # 恢复播报用反查后的详情 (含最新 last(2))
                events.append(self._make_event("RECOVERED", aid, cached, ts, speech_alarm=detail))
            else:
                del self._known[aid]
                events.append(self._make_event("DISMISSED", aid, cached, ts))

        return events

    @staticmethod
    def _make_event(
        kind: str,
        aid,
        alarm: dict,
        ts: str,
        speech_alarm: Optional[dict] = None,
    ) -> AlarmEvent:
        src = speech_alarm if speech_alarm is not None else alarm
        return AlarmEvent(
            kind=kind,
            alarm_id=aid,
            device=alarm.get("instance_name", "未知设备"),
            ip=alarm.get("ip", ""),
            title=alarm.get("alarm_title", "未命名告警"),
            timestamp=ts,
            alarm=dict(src),
        )
