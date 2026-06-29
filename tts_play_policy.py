"""
播报策略执行器 — 控制同一告警是否重复播报
==========================================
对接 flet_demo 左栏「出现告警时」下拉框。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


# 与 flet_demo 中常量保持一致
PLAY_POLICY_ONCE = "once"
PLAY_POLICY_REPEAT_N = "repeat_n"
PLAY_POLICY_UNTIL_RECOVER = "until_recover"
PLAY_POLICY_UNTIL_COOLDOWN = "until_recover_cooldown"
PLAY_POLICY_INTERVAL = "interval_repeat"


@dataclass
class PlayPolicyConfig:
    """播报策略配置。"""

    policy_id: str = PLAY_POLICY_ONCE
    repeat_n: int = 3
    cooldown_seconds: int = 120
    interval_minutes: int = 5


@dataclass
class _AlarmPlayState:
    play_count: int = 0
    last_play_at: float = 0.0


class PlayPolicyExecutor:
    """
    判断某告警事件是否应触发语音播报。
    同一 alarm_id 的 RECOVERED 事件始终允许播报（恢复话术）。
    """

    def __init__(self, config: Optional[PlayPolicyConfig] = None) -> None:
        self.config = config or PlayPolicyConfig()
        self._states: dict[Any, _AlarmPlayState] = {}

    def update_config(self, config: PlayPolicyConfig) -> None:
        self.config = config

    def reset_alarm(self, alarm_id: Any) -> None:
        self._states.pop(alarm_id, None)

    def should_play(self, alarm_id: Any, event_type: str) -> bool:
        """
        :param alarm_id: 告警 ID
        :param event_type: NEW / RECOVERED
        """
        if event_type == "RECOVERED":
            self.reset_alarm(alarm_id)
            return True

        if event_type != "NEW":
            return False

        cfg = self.config
        now = time.time()
        st = self._states.setdefault(alarm_id, _AlarmPlayState())

        if cfg.policy_id == PLAY_POLICY_ONCE:
            if st.play_count >= 1:
                return False
            st.play_count += 1
            st.last_play_at = now
            return True

        if cfg.policy_id == PLAY_POLICY_REPEAT_N:
            if st.play_count >= max(1, cfg.repeat_n):
                return False
            st.play_count += 1
            st.last_play_at = now
            return True

        if cfg.policy_id == PLAY_POLICY_UNTIL_RECOVER:
            st.play_count += 1
            st.last_play_at = now
            return True

        if cfg.policy_id == PLAY_POLICY_UNTIL_COOLDOWN:
            if st.last_play_at > 0 and (now - st.last_play_at) < max(1, cfg.cooldown_seconds):
                return False
            st.play_count += 1
            st.last_play_at = now
            return True

        if cfg.policy_id == PLAY_POLICY_INTERVAL:
            interval_sec = max(1, cfg.interval_minutes) * 60
            if st.last_play_at > 0 and (now - st.last_play_at) < interval_sec:
                return False
            st.play_count += 1
            st.last_play_at = now
            return True

        # 未知策略：仅播一次
        if st.play_count >= 1:
            return False
        st.play_count += 1
        st.last_play_at = now
        return True

    def is_repeat_play(self, alarm_id: Any) -> bool:
        """当前告警是否属于第 2 次及以后的播报（可走 L2 整句缓存）。"""
        st = self._states.get(alarm_id)
        return st is not None and st.play_count > 1
