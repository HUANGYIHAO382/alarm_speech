"""
告警视听助手 - UI 入口
    1. 搭建 Flet 界面（参照 docs/UI优化方案.md v3）
    2. 在用户操作时调用 api_client / alarm_processor / tts_engine
    3. 把后台线程的结果刷新到界面
    4. 运行日志落盘至 logs/（见 app_logger.py）
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import flet as ft

from api_client import AlarmApiClient
from alarm_processor import (
    PORT_STATE_TTS_CN,
    RULE_AUTO,
    SPEECH_RULE_OPTIONS,
    AlarmTracker,
    alarm_to_speech,
    build_port_table_speech_text,
    event_to_speech,
    format_alarm_for_active_console,
    format_alarm_for_console,
    parse_last2_state,
    polish_speech_for_tts,
    speech_rule_label,
)
from app_logger import (
    get_logger,
    init_app_logging,
    log_exception,
    log_ui_action,
    session_log_path,
    tail_log_lines,
)
from tts_config import (
    MOSS_CPU_PRESET_RESERVE1,
    MOSS_CPU_THREAD_PRESETS,
    MOSS_DEVICE_CPU,
    MOSS_DEVICE_GPU,
    MOSS_DEVICE_LABELS,
    logical_cpu_count,
    moss_cpu_threads_label,
    probe_moss_cuda_detailed,
    resolve_moss_cpu_threads,
)
from tts_engine import BACKEND_LOCAL, BACKEND_MOSS, BACKEND_XFYUN, get_default_engine

# 终端欢迎语
WELCOME_TEXT = """告警视听助手已就绪
======================================================================

操作指引:
  1. 填写 HOST / Token
  2. [历史告警]  拉取最近 100 条全量记录
  3. 选间隔 + [开始轮询]  自动追踪新增/恢复并语音播报
  4. 选择 [播报规则] + [测试语音] 验证转换结果

等待操作...
"""

# ---------- 设计令牌（docs/UI优化方案.md）----------
LEFT_PANEL_WIDTH = 500
CTRL_HEIGHT = 38
CTRL_RADIUS = 8
CTRL_TEXT_SIZE = 12
SECTION_SPACING = 10
FIELD_SPACING = 12
CARD_RADIUS = 12

# 色彩
C_BG = "#0f1117"
C_SURFACE = "#181c24"
C_SURFACE_ALT = "#1f2530"
C_BORDER = "#2a3140"
C_BORDER_FOCUS = "#4d8bf7"
C_ACCENT = "#5b9cf5"
C_ACCENT_SOFT = "#1a2d4a"
C_SUCCESS = "#6ecf8a"
C_WARN = "#f0b45c"
C_DANGER = "#e85d5d"
C_TEXT = "#e8eaed"
C_TEXT_MUTED = "#8b93a7"
C_TERMINAL_BG = "#080a0e"
C_TERMINAL_TEXT = "#5ecf7a"

# 告警播报策略（UI 已就绪，后端逻辑 Phase 2 接入）
PLAY_POLICY_ONCE = "once"
PLAY_POLICY_REPEAT_N = "repeat_n"
PLAY_POLICY_UNTIL_RECOVER = "until_recover"
PLAY_POLICY_UNTIL_COOLDOWN = "until_recover_cooldown"
PLAY_POLICY_INTERVAL = "interval_repeat"

PLAY_POLICY_OPTIONS = [
    (PLAY_POLICY_ONCE, "仅播一次"),
    (PLAY_POLICY_REPEAT_N, "播报 N 次后停止"),
    (PLAY_POLICY_UNTIL_RECOVER, "持续直至恢复"),
    (PLAY_POLICY_UNTIL_COOLDOWN, "持续直至恢复（冷却）"),
    (PLAY_POLICY_INTERVAL, "每 X 分钟重复"),
]


def _field_border() -> dict:
    """输入框/下拉统一边框色。"""
    return {
        "border_color": C_BORDER,
        "focused_border_color": C_ACCENT,
        "border_radius": CTRL_RADIUS,
        "bgcolor": C_SURFACE_ALT,
        "color": C_TEXT,
    }


def _base_button_style(
    *,
    text_color: str = C_TEXT,
    side_color: str | None = None,
) -> ft.ButtonStyle:
    return ft.ButtonStyle(
        padding=ft.padding.symmetric(horizontal=14, vertical=10),
        shape=ft.RoundedRectangleBorder(radius=CTRL_RADIUS),
        text_style=ft.TextStyle(size=CTRL_TEXT_SIZE, weight=ft.FontWeight.W_500, color=text_color),
        side=ft.BorderSide(1, side_color) if side_color else None,
    )


def _action_btn(text: str, icon, bgcolor: str, on_click) -> ft.ElevatedButton:
    return ft.ElevatedButton(
        text=text,
        icon=icon,
        on_click=on_click,
        height=CTRL_HEIGHT,
        width=float("inf"),
        color=ft.Colors.WHITE,
        bgcolor=bgcolor,
        style=_base_button_style(),
    )


def _outline_btn(
    text: str,
    icon,
    on_click,
    *,
    text_color: str,
    border_color: str,
) -> ft.OutlinedButton:
    return ft.OutlinedButton(
        text=text,
        icon=icon,
        on_click=on_click,
        height=CTRL_HEIGHT,
        width=float("inf"),
        style=_base_button_style(text_color=text_color, side_color=border_color),
    )


def _compact_field(field: ft.Control) -> ft.Control:
    border = _field_border()
    if isinstance(field, ft.TextField):
        field.text_size = CTRL_TEXT_SIZE
        field.content_padding = 12
        field.dense = True
        for k, v in border.items():
            setattr(field, k, v)
    elif isinstance(field, ft.Dropdown):
        field.text_size = CTRL_TEXT_SIZE
        field.dense = True
        field.content_padding = ft.padding.symmetric(horizontal=12, vertical=10)
        for k, v in border.items():
            setattr(field, k, v)
    return field


def _subsection_label(text: str, icon=None) -> ft.Row:
    """分区小标题（监控与语音内部）。"""
    items = []
    if icon is not None:
        items.append(ft.Icon(icon, size=14, color=C_ACCENT))
    items.append(ft.Text(text, size=11, weight=ft.FontWeight.W_600, color=C_TEXT_MUTED))
    return ft.Row(controls=items, spacing=6)


def _hint_chip(text: str, *, ok: bool = True) -> ft.Container:
    """一行提示（CUDA / 引擎状态）。"""
    return ft.Container(
        content=ft.Text(text, size=10, color=C_SUCCESS if ok else C_WARN),
        bgcolor="#1e3d2a22" if ok else "#3d2e1a33",
        border=ft.border.all(1, "#2e5c3e55" if ok else "#5c4a2e55"),
        border_radius=6,
        padding=ft.padding.symmetric(horizontal=10, vertical=6),
    )


def _panel_card(
    title: str,
    icon,
    body: ft.Control,
    *,
    accent: str = C_ACCENT,
) -> ft.Container:
    """带左侧色条的现代卡片容器。"""
    header = ft.Row(
        controls=[
            ft.Container(
                width=4,
                height=18,
                bgcolor=accent,
                border_radius=2,
            ),
            ft.Icon(icon, size=16, color=accent),
            ft.Text(title, size=13, weight=ft.FontWeight.W_600, color=C_TEXT),
        ],
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
    return ft.Container(
        content=ft.Column(controls=[header, body], spacing=FIELD_SPACING),
        bgcolor=C_SURFACE,
        border=ft.border.all(1, C_BORDER),
        border_radius=CARD_RADIUS,
        padding=ft.padding.all(14),
    )


def _status_dot(active: bool, *, danger: bool = False) -> ft.Container:
    color = C_DANGER if danger else (C_SUCCESS if active else C_TEXT_MUTED)
    return ft.Container(
        width=8,
        height=8,
        bgcolor=color,
        border_radius=4,
    )


def main(page: ft.Page):
    init_app_logging()
    logger = get_logger("flet_demo")
    logger.info("告警视听助手 UI 启动")

    page.title = "告警视听助手"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0
    page.bgcolor = C_BG
    page.window_width = 1360
    page.window_height = 780

    tts = get_default_engine()
    tracker = AlarmTracker(announce_existing=False)
    poll_state = {"active": False, "interval_seconds": 30}
    event_log: list[str] = []
    ui_cache = {"last_history_alarms": []}

    def get_api_client() -> AlarmApiClient:
        return AlarmApiClient(input_host.value, input_token.value)

    def get_speech_rule_id() -> str:
        return input_speech_rule.value or RULE_AUTO

    def get_play_policy_label() -> str:
        pid = input_play_policy.value or PLAY_POLICY_ONCE
        for rid, label in PLAY_POLICY_OPTIONS:
            if rid == pid:
                return label
        return pid

    def append_console(text: str) -> None:
        console_output.value = text
        for line in text.strip().splitlines()[-5:]:
            if line.strip():
                logger.info("[终端] %s", line.strip()[:500])

    # ---------- 连接配置 ----------
    input_host = _compact_field(ft.TextField(
        label="HOST", value="",
        hint_text="http://监控服务器",
        hint_style=ft.TextStyle(color=C_TEXT_MUTED),
    ))
    input_token = _compact_field(ft.TextField(
        label="Token", value="",
        hint_text="访问令牌",
        hint_style=ft.TextStyle(color=C_TEXT_MUTED),
        password=True,
        can_reveal_password=True,
    ))
    conn_fields = ft.Column(controls=[input_host, input_token], spacing=FIELD_SPACING)

    # ---------- 轮询 ----------
    input_timer = _compact_field(ft.Dropdown(
        label="轮询间隔",
        options=[
            ft.dropdown.Option("30", "30 秒"),
            ft.dropdown.Option("60", "1 分钟"),
        ],
        value="30",
    ))

    # ---------- 语音引擎与文本规则 ----------
    input_speech_rule = _compact_field(ft.Dropdown(
        label="文本规则",
        options=[ft.dropdown.Option(rid, label) for rid, label in SPEECH_RULE_OPTIONS],
        value=RULE_AUTO,
    ))
    input_tts_backend = _compact_field(ft.Dropdown(
        label="语音引擎",
        options=[
            ft.dropdown.Option(BACKEND_LOCAL, "Windows SAPI"),
            ft.dropdown.Option(BACKEND_MOSS, "MOSS 本地"),
            ft.dropdown.Option(BACKEND_XFYUN, "讯飞在线"),
        ],
        value=tts.get_backend(),
    ))
    tts_hint_text = ft.Text("", color=C_WARN, size=10)

    _moss_cuda_ok, _moss_cuda_detail = probe_moss_cuda_detailed()
    input_moss_device = _compact_field(ft.Dropdown(
        label="MOSS 加速",
        options=[
            ft.dropdown.Option(MOSS_DEVICE_GPU, MOSS_DEVICE_LABELS[MOSS_DEVICE_GPU]),
            ft.dropdown.Option(MOSS_DEVICE_CPU, MOSS_DEVICE_LABELS[MOSS_DEVICE_CPU]),
        ],
        value=tts.get_moss_execution_provider(),
    ))
    moss_device_hint = _hint_chip(
        "CUDA 已验证，推荐 GPU" if _moss_cuda_ok else f"GPU 未就绪，可用 CPU 或运行 fix_moss_gpu.ps1",
        ok=_moss_cuda_ok,
    )
    # 纯 CPU 时：与「MOSS 加速」并排，设置 ONNX 算子内并行线程（见 MOSS语音缓存与拼合方案 五-C）
    input_moss_cpu_threads = _compact_field(ft.Dropdown(
        label="CPU 并行",
        options=[ft.dropdown.Option(pid, label) for pid, label in MOSS_CPU_THREAD_PRESETS],
        value=MOSS_CPU_PRESET_RESERVE1,
    ))
    moss_cpu_threads_hint = ft.Text(
        f"本机 {logical_cpu_count()} 逻辑核，默认留 1 核给系统与界面",
        size=9,
        color=C_TEXT_MUTED,
    )
    moss_cpu_threads_col = ft.Column(
        controls=[input_moss_cpu_threads, moss_cpu_threads_hint],
        spacing=4,
    )
    moss_accel_row = ft.Row(
        controls=[
            ft.Container(content=input_moss_device, expand=1),
            ft.Container(content=moss_cpu_threads_col, expand=1),
        ],
        spacing=SECTION_SPACING,
        vertical_alignment=ft.CrossAxisAlignment.START,
    )
    moss_device_row = ft.Container(content=moss_accel_row)

    moss_progress_bar = ft.ProgressBar(value=0, bar_height=5, color=C_ACCENT, bgcolor=C_BORDER)
    moss_progress_text = ft.Text("", size=10, color=C_TEXT_MUTED)
    moss_progress_panel = ft.Column(
        controls=[moss_progress_text, moss_progress_bar],
        spacing=6,
        visible=False,
    )

    # ---------- 告警播报策略（UI）----------
    input_play_policy = _compact_field(ft.Dropdown(
        label="出现告警时",
        options=[ft.dropdown.Option(rid, label) for rid, label in PLAY_POLICY_OPTIONS],
        value=PLAY_POLICY_ONCE,
    ))
    input_policy_repeat_n = _compact_field(ft.TextField(
        label="播报次数 N", value="3", keyboard_type=ft.KeyboardType.NUMBER,
    ))
    input_policy_cooldown = _compact_field(ft.TextField(
        label="冷却秒数", value="120", keyboard_type=ft.KeyboardType.NUMBER,
    ))
    input_policy_interval = _compact_field(ft.TextField(
        label="重复间隔（分钟）", value="5", keyboard_type=ft.KeyboardType.NUMBER,
    ))
    policy_params_repeat = ft.Container(content=input_policy_repeat_n, visible=False)
    policy_params_cooldown = ft.Container(content=input_policy_cooldown, visible=False)
    policy_params_interval = ft.Container(content=input_policy_interval, visible=False)

    def _sync_play_policy_panels() -> None:
        pid = input_play_policy.value or PLAY_POLICY_ONCE
        policy_params_repeat.visible = pid == PLAY_POLICY_REPEAT_N
        policy_params_cooldown.visible = pid in (
            PLAY_POLICY_UNTIL_COOLDOWN,
            PLAY_POLICY_UNTIL_RECOVER,
        )
        policy_params_interval.visible = pid == PLAY_POLICY_INTERVAL

    def on_play_policy_change(e):
        _sync_play_policy_panels()
        page.update()

    input_play_policy.on_change = on_play_policy_change

    # ---------- SAPI 语速 ----------
    def _local_rate_label(rate: int) -> str:
        if rate <= -5:
            hint = "很慢"
        elif rate <= -2:
            hint = "较慢"
        elif rate <= 2:
            hint = "正常"
        elif rate <= 5:
            hint = "较快"
        else:
            hint = "很快"
        return f"语速 {rate} · {hint}"

    slider_local_rate = ft.Slider(
        min=-10, max=10, divisions=20,
        value=tts.get_local_rate(),
        active_color=C_ACCENT,
        inactive_color=C_BORDER,
    )
    local_rate_text = ft.Text(_local_rate_label(tts.get_local_rate()), size=10, color=C_TEXT_MUTED)
    local_rate_panel = ft.Column(
        controls=[local_rate_text, slider_local_rate],
        spacing=4,
        visible=(tts.get_backend() == BACKEND_LOCAL),
    )

    def on_local_rate_change(e):
        rate = int(round(slider_local_rate.value))
        tts.set_local_rate(rate)
        local_rate_text.value = _local_rate_label(rate)
        page.update()

    slider_local_rate.on_change = on_local_rate_change

    switch_voice = ft.Switch(
        label="启用语音播报",
        value=True,
        active_color=C_SUCCESS,
        active_track_color="#2a4a35",
        label_style=ft.TextStyle(size=CTRL_TEXT_SIZE, color=C_TEXT),
        on_change=lambda e: tts.set_enabled(switch_voice.value),
    )

    input_preview_text = _compact_field(ft.TextField(
        label="试听文字",
        value=build_port_table_speech_text("B05_NE20E-1", "up"),
        multiline=True,
        min_lines=2,
        max_lines=3,
    ))
    btn_preview = _outline_btn(
        "试听语音", ft.Icons.GRAPHIC_EQ, None,
        text_color=C_ACCENT, border_color=C_ACCENT,
    )

    def _apply_moss_cpu_threads() -> None:
        """把下拉预设转为线程数并交给 TTS 引擎（会重启守护进程）。"""
        preset = input_moss_cpu_threads.value or MOSS_CPU_PRESET_RESERVE1
        threads = resolve_moss_cpu_threads(preset)
        tts.set_moss_cpu_threads(threads)
        moss_cpu_threads_hint.value = (
            f"当前 {moss_cpu_threads_label(threads)} · 本机共 {logical_cpu_count()} 逻辑核"
        )

    def _sync_moss_panels() -> None:
        is_moss = tts.get_backend() == BACKEND_MOSS
        is_cpu = tts.get_moss_execution_provider() == MOSS_DEVICE_CPU
        moss_device_row.visible = is_moss
        moss_device_hint.visible = is_moss
        moss_cpu_threads_col.visible = is_moss and is_cpu
        if not is_moss:
            moss_progress_panel.visible = False

    def _update_moss_progress(phase: str, elapsed: float, msg: str) -> None:
        if tts.get_backend() != BACKEND_MOSS:
            return
        moss_progress_panel.visible = True
        if phase == "done":
            moss_progress_bar.value = 1.0
        elif phase in ("loading", "daemon"):
            moss_progress_bar.value = min(0.8, max(0.05, elapsed / 120.0))
        elif phase == "synthesize":
            moss_progress_bar.value = min(0.95, 0.8 + elapsed / 60.0)
        else:
            moss_progress_bar.value = min(0.9, max(0.1, elapsed / 90.0))
        moss_progress_text.value = f"{msg}  ·  {elapsed:.1f}s"
        try:
            page.update()
        except Exception:
            pass

    tts.set_progress_callback(_update_moss_progress)

    def on_moss_device_change(e):
        err = tts.set_moss_execution_provider(input_moss_device.value)
        if err:
            moss_device_hint.content = ft.Text(f"切换失败: {err}", size=10, color=C_WARN)
        else:
            mode = MOSS_DEVICE_LABELS.get(input_moss_device.value, input_moss_device.value)
            moss_device_hint.content = ft.Text(f"当前 {mode}，切换后首次试听会加载模型", size=10, color=C_SUCCESS)
            logger.info("MOSS 设备模式: %s", input_moss_device.value)
        _sync_moss_panels()
        if input_moss_device.value == MOSS_DEVICE_CPU:
            _apply_moss_cpu_threads()
        page.update()

    def on_moss_cpu_threads_change(e):
        _apply_moss_cpu_threads()
        logger.info("MOSS CPU 线程: %s", tts.get_moss_cpu_threads())
        page.update()

    input_moss_device.on_change = on_moss_device_change
    input_moss_cpu_threads.on_change = on_moss_cpu_threads_change
    if not _moss_cuda_ok and tts.get_moss_execution_provider() == MOSS_DEVICE_GPU:
        tts.set_moss_execution_provider(MOSS_DEVICE_CPU)
        input_moss_device.value = MOSS_DEVICE_CPU
    _apply_moss_cpu_threads()
    _sync_moss_panels()
    _sync_play_policy_panels()

    def _update_preview_labels() -> None:
        label = tts.backend_label()
        btn_preview.text = f"试听 · {label}"
        input_preview_text.label = f"试听文字 · {label}"

    def on_click_preview(e):
        raw = (input_preview_text.value or "").strip()
        if not raw:
            append_console("请在试听文字框中输入要播报的内容")
            page.update()
            return
        btn_preview.disabled = True
        page.update()

        def _worker():
            polished = polish_speech_for_tts(raw)
            engine_label = tts.backend_label()
            now = datetime.now().strftime("%H:%M:%S")
            moss_mode = ""
            if tts.get_backend() == BACKEND_MOSS:
                moss_mode = MOSS_DEVICE_LABELS.get(tts.get_moss_execution_provider(), "")

            log_ui_action(logger, "试听语音", f"引擎={engine_label} {moss_mode} 原文={raw[:80]}")
            out = f"[{now}] 试听 · {engine_label}\n{'=' * 70}\n"
            out += f"原始: {raw}\n"
            if polished != raw:
                out += f"润色: {polished}\n"
            if moss_mode:
                out += f"MOSS: {moss_mode}\n"
            out += f"{'-' * 70}\n处理中...\n"
            append_console(out)
            set_status(f"试听 · {engine_label}", busy=True)
            page.update()

            err = tts.speak_preview_blocking(polished)
            out += f"失败: {err}\n" if err else "试听完成\n"
            moss_progress_panel.visible = False
            btn_preview.disabled = False
            append_console(out)
            set_status("待命")
            page.update()

        threading.Thread(target=_worker, daemon=True, name="preview-worker").start()

    btn_preview.on_click = on_click_preview

    def on_tts_backend_change(e):
        err = tts.set_backend(input_tts_backend.value)
        if err:
            tts_hint_text.value = err
            input_tts_backend.value = tts.get_backend()
        else:
            tts_hint_text.value = ""
            logger.info("TTS 引擎: %s", tts.backend_label())
        local_rate_panel.visible = tts.get_backend() == BACKEND_LOCAL
        top_tts_chip.content.value = tts.backend_label()
        _sync_moss_panels()
        _update_preview_labels()
        page.update()

    input_tts_backend.on_change = on_tts_backend_change
    _update_preview_labels()

    # ---------- 状态（顶栏与侧栏底栏各一份，由 set_status 同步）----------
    status_text = ft.Text("待命", size=12, weight=ft.FontWeight.W_500, color=C_TEXT)
    status_dot = _status_dot(False)
    status_text_bar = ft.Text("待命", size=12, weight=ft.FontWeight.W_500, color=C_TEXT)
    status_dot_bar = _status_dot(False)
    top_tts_chip = ft.Container(
        content=ft.Text(tts.backend_label(), size=11, color=C_ACCENT),
        bgcolor=C_ACCENT_SOFT,
        border=ft.border.all(1, "#3d5a80"),
        border_radius=20,
        padding=ft.padding.symmetric(horizontal=10, vertical=4),
    )
    track_badge = ft.Container(
        content=ft.Text("追踪 0", size=10, color=C_ACCENT),
        bgcolor=C_ACCENT_SOFT,
        border_radius=20,
        padding=ft.padding.symmetric(horizontal=10, vertical=4),
        border=ft.border.all(1, "#3d5a80"),
    )
    log_path_text = ft.Text(
        f"日志 {session_log_path()}",
        size=9,
        color=C_TEXT_MUTED,
        tooltip="落盘日志路径",
    )

    def set_status(msg: str, *, busy: bool = False, danger: bool = False):
        status_text.value = msg
        status_text_bar.value = msg
        dot_color = C_DANGER if danger else (C_WARN if busy else C_SUCCESS if poll_state["active"] else C_TEXT_MUTED)
        status_dot.bgcolor = dot_color
        status_dot_bar.bgcolor = dot_color

    # ---------- 终端 ----------
    console_output = ft.TextField(
        multiline=True,
        read_only=True,
        expand=True,
        value=WELCOME_TEXT,
        text_size=12,
        text_style=ft.TextStyle(font_family="Consolas", color=C_TERMINAL_TEXT),
        border=ft.InputBorder.NONE,
        bgcolor="transparent",
        cursor_color=C_TERMINAL_TEXT,
    )

    def push_event_log(line: str) -> None:
        event_log.insert(0, line)
        del event_log[40:]
        logger.info("[事件] %s", line)

    def update_track_badge():
        track_badge.content.value = f"追踪 {tracker.tracking_count()}"
        track_badge.update()

    def render_polling_view(active_list, refresh_time: str) -> str:
        policy = get_play_policy_label()
        out = "事件播报日志 (最新在上)\n"
        out += "=" * 70 + "\n"
        out += ("\n".join(event_log) if event_log else "（暂无新增 / 恢复事件）") + "\n"
        out += "=" * 70 + "\n\n"
        out += f"[{refresh_time}] 活跃告警 {len(active_list)} 条  |  播报策略: {policy}\n"
        out += "-" * 70 + "\n\n"
        for alarm in active_list:
            out += format_alarm_for_active_console(alarm) + ("-" * 70) + "\n\n"
        return out

    # ---------- 历史 ----------
    def on_click_history(e):
        try:
            log_ui_action(logger, "拉取历史告警")
            client = get_api_client()
            if not client.host or not client.token:
                append_console("错误: HOST 或 Token 不能为空")
                page.update()
                return
            append_console("正在请求最近 100 条历史记录...\n")
            page.update()
            result = client.fetch_history(limit=100)
            if not result.ok:
                append_console(f"失败: {result.error}")
                page.update()
                return
            ui_cache["last_history_alarms"] = result.data
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            out = f"[{now}] 历史拉取成功 | 共 {len(result.data)} 条\n{'=' * 70}\n\n"
            for idx, alarm in enumerate(result.data, 1):
                out += format_alarm_for_console(alarm, idx=idx) + ("-" * 70) + "\n\n"
            append_console(out)
            set_status("已拉取历史")
            page.update()
        except Exception as ex:
            append_console(log_exception(logger, "历史拉取异常", ex))
            page.update()

    # ---------- 轮询逻辑 ----------
    def poll_once():
        try:
            client = get_api_client()
            if not client.host or not client.token:
                return
            result = client.fetch_active_alarms(limit=50)
            if not result.ok:
                set_status(f"轮询失败: {result.error}", danger=True)
                page.update()
                return
            events = tracker.diff(result.data, query_by_id=client.query_by_id)
            for ev in events:
                push_event_log(ev.log_line)
                speech = event_to_speech(ev, get_speech_rule_id())
                if speech:
                    tts.speak(speech)
                    err = tts.last_error()
                    if err:
                        logger.error("播报失败: %s", err)
            refresh_time = datetime.now().strftime("%H:%M:%S")
            append_console(render_polling_view(result.data, refresh_time))
            set_status(f"轮询中 {poll_state['interval_seconds']}s · {refresh_time}")
            update_track_badge()
            page.update()
        except Exception as ex:
            log_exception(logger, "轮询异常", ex)
            set_status("轮询异常", danger=True)
            page.update()

    btn_poll = _action_btn("开始轮询", ft.Icons.PLAY_ARROW, C_ACCENT, None)

    def on_click_poll_toggle(e):
        if poll_state["active"]:
            poll_state["active"] = False
            btn_poll.text = "开始轮询"
            btn_poll.icon = ft.Icons.PLAY_ARROW
            btn_poll.bgcolor = C_ACCENT
            input_timer.disabled = False
            set_status("待命")
            log_ui_action(logger, "停止轮询")
        else:
            poll_state["interval_seconds"] = int(input_timer.value)
            poll_state["active"] = True
            btn_poll.text = "停止轮询"
            btn_poll.icon = ft.Icons.STOP_ROUNDED
            btn_poll.bgcolor = C_DANGER
            input_timer.disabled = True
            set_status(f"轮询中 {poll_state['interval_seconds']}s")
            log_ui_action(logger, "开始轮询", f"间隔={poll_state['interval_seconds']}s")
            poll_once()
        btn_poll.update()
        input_timer.update()
        page.update()

    btn_poll.on_click = on_click_poll_toggle

    def background_polling_task():
        while True:
            try:
                if poll_state["active"]:
                    for _ in range(poll_state["interval_seconds"]):
                        if not poll_state["active"]:
                            break
                        time.sleep(1)
                    if poll_state["active"]:
                        poll_once()
                else:
                    time.sleep(1)
            except Exception as ex:
                log_exception(logger, "后台轮询线程异常", ex)
                time.sleep(5)

    threading.Thread(target=background_polling_task, daemon=True, name="poll-worker").start()

    def on_click_test_speech(e):
        try:
            log_ui_action(logger, "测试语音")
            alarms = ui_cache["last_history_alarms"]
            if not alarms:
                client = get_api_client()
                if not client.host or not client.token:
                    append_console("请先填写 HOST / Token，或先拉取历史")
                    page.update()
                    return
                result = client.fetch_history(limit=1)
                if not result.ok or not result.data:
                    append_console(f"无法获取测试数据: {result.error}")
                    page.update()
                    return
                alarms = result.data
                ui_cache["last_history_alarms"] = alarms
            alarm = alarms[0]
            rule_id = get_speech_rule_id()
            speech_text = alarm_to_speech(alarm, rule_id)
            now = datetime.now().strftime("%H:%M:%S")
            out = f"[{now}] 语音测试\n规则: {speech_rule_label(rule_id)} | 策略: {get_play_policy_label()}\n"
            out += f"播报: {speech_text}\n引擎: {tts.backend_label()}\n{'-' * 70}\n"
            append_console(out + "推送中...\n")
            set_status("测试语音", busy=True)
            page.update()
            tts.speak(speech_text)
            err = tts.last_error()
            out += f"失败: {err}\n" if err else "已推送\n"
            append_console(out)
            set_status("待命")
            page.update()
        except Exception as ex:
            append_console(log_exception(logger, "测试语音异常", ex))
            page.update()

    btn_history = _outline_btn(
        "历史 100 条", ft.Icons.HISTORY, on_click_history,
        text_color=C_WARN, border_color="#8a6040",
    )
    btn_test_speech = _outline_btn(
        "测试语音", ft.Icons.RECORD_VOICE_OVER, on_click_test_speech,
        text_color=C_SUCCESS, border_color="#408a55",
    )

    def on_clear_console(e):
        append_console(WELCOME_TEXT)
        page.update()

    def on_show_log_file(e):
        log_ui_action(logger, "查看落盘日志")
        append_console(
            f"落盘日志末尾 ({session_log_path()})\n{'=' * 70}\n{tail_log_lines(100)}"
        )
        page.update()

    poll_row = ft.Row(
        controls=[
            ft.Container(content=input_timer, expand=3),
            ft.Container(content=btn_poll, expand=2),
        ],
        spacing=SECTION_SPACING,
        vertical_alignment=ft.CrossAxisAlignment.END,
    )
    voice_dropdown_row = ft.Row(
        controls=[
            ft.Container(content=input_tts_backend, expand=1),
            ft.Container(content=input_speech_rule, expand=1),
        ],
        spacing=SECTION_SPACING,
    )
    manual_row = ft.Row(
        controls=[
            ft.Container(content=btn_history, expand=1),
            ft.Container(content=btn_test_speech, expand=1),
        ],
        spacing=SECTION_SPACING,
    )

    monitor_body = ft.Column(
        controls=[
            _subsection_label("轮询监控", ft.Icons.RADAR),
            poll_row,
            ft.Divider(height=1, color=C_BORDER),
            _subsection_label("试听", ft.Icons.GRAPHIC_EQ),
            input_preview_text,
            btn_preview,
            moss_progress_panel,
            ft.Divider(height=1, color=C_BORDER),
            _subsection_label("引擎与规则", ft.Icons.TUNE),
            voice_dropdown_row,
            moss_device_row,
            moss_device_hint,
            local_rate_panel,
            tts_hint_text,
            ft.Divider(height=1, color=C_BORDER),
            _subsection_label("告警播报策略", ft.Icons.CAMPAIGN),
            input_play_policy,
            policy_params_repeat,
            policy_params_cooldown,
            policy_params_interval,
            ft.Divider(height=1, color=C_BORDER),
            switch_voice,
            manual_row,
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Row(
                            controls=[status_dot, status_text, ft.Container(expand=True), track_badge],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        log_path_text,
                    ],
                    spacing=6,
                ),
                bgcolor=C_SURFACE_ALT,
                border_radius=CTRL_RADIUS,
                padding=ft.padding.all(10),
                border=ft.border.all(1, C_BORDER),
            ),
        ],
        spacing=FIELD_SPACING,
    )

    # ---------- 顶栏 ----------
    top_bar = ft.Container(
        content=ft.Row(
            controls=[
                ft.Icon(ft.Icons.NOTIFICATIONS_ACTIVE, color=C_ACCENT, size=22),
                ft.Text("告警视听助手", size=16, weight=ft.FontWeight.W_600, color=C_TEXT),
                ft.Container(width=12),
                ft.Row(controls=[status_dot_bar, status_text_bar], spacing=8),
                ft.Container(expand=True),
                top_tts_chip,
                ft.IconButton(
                    icon=ft.Icons.DESCRIPTION_OUTLINED,
                    icon_color=C_TEXT_MUTED,
                    icon_size=20,
                    tooltip="查看落盘日志",
                    on_click=on_show_log_file,
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor=C_SURFACE,
        border=ft.border.only(bottom=ft.BorderSide(1, C_BORDER)),
        padding=ft.padding.symmetric(horizontal=20, vertical=12),
    )

    left_panel = ft.Container(
        content=ft.Column(
            controls=[
                _panel_card("连接配置", ft.Icons.LINK, conn_fields, accent=C_ACCENT),
                _panel_card("监控与语音", ft.Icons.SETTINGS_VOICE, monitor_body, accent=C_SUCCESS),
            ],
            spacing=SECTION_SPACING,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        ),
        width=LEFT_PANEL_WIDTH,
        padding=ft.padding.all(14),
    )

    terminal_header = ft.Row(
        controls=[
            ft.Row(
                controls=[
                    ft.Container(width=10, height=10, bgcolor="#ff5f57", border_radius=5),
                    ft.Container(width=10, height=10, bgcolor="#febc2e", border_radius=5),
                    ft.Container(width=10, height=10, bgcolor="#28c840", border_radius=5),
                ],
                spacing=6,
            ),
            ft.Container(width=8),
            ft.Icon(ft.Icons.TERMINAL, color=C_TEXT_MUTED, size=16),
            ft.Text("数据终端", size=13, weight=ft.FontWeight.W_600, color=C_TEXT),
            ft.Container(expand=True),
            ft.IconButton(
                icon=ft.Icons.DELETE_SWEEP_OUTLINED,
                icon_size=18,
                icon_color=C_TEXT_MUTED,
                tooltip="清空终端",
                on_click=on_clear_console,
            ),
        ],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    right_panel = ft.Container(
        content=ft.Column(
            controls=[
                terminal_header,
                ft.Divider(height=1, color=C_BORDER),
                ft.Container(content=console_output, expand=True, padding=ft.padding.all(4)),
            ],
            expand=True,
            spacing=8,
        ),
        bgcolor=C_TERMINAL_BG,
        border=ft.border.all(1, C_BORDER),
        border_radius=CARD_RADIUS,
        padding=ft.padding.all(14),
        expand=True,
    )

    page.add(
        ft.Column(
            controls=[
                top_bar,
                ft.Container(
                    content=ft.Row(
                        controls=[left_panel, right_panel],
                        expand=True,
                        spacing=12,
                    ),
                    expand=True,
                    padding=ft.padding.all(10),
                ),
            ],
            expand=True,
            spacing=0,
        )
    )


if __name__ == "__main__":
    init_app_logging()
    get_logger("flet_demo").info("通过 __main__ 启动 Flet")
    ft.app(target=main)
