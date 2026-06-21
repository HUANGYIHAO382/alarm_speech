"""
告警视听助手 - UI 入口
    1. 搭建 Flet 界面
    2. 在用户操作时调用 api_client / alarm_processor / tts_engine
    3. 把后台线程的结果刷新到界面
"""
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
from tts_engine import BACKEND_LOCAL, BACKEND_XFYUN, get_default_engine

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


def _section_title(text: str) -> ft.Text:
    return ft.Text(text, size=11, color="#888888", weight=ft.FontWeight.W_500)


def _action_btn(text: str, icon, bgcolor: str, on_click) -> ft.ElevatedButton:
    """紧凑型操作按钮, 高度 34px。"""
    return ft.ElevatedButton(
        text=text, icon=icon, on_click=on_click,
        height=34, color=ft.colors.WHITE, bgcolor=bgcolor,
        style=ft.ButtonStyle(
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
            shape=ft.RoundedRectangleBorder(radius=4),
            text_style=ft.TextStyle(size=12),
        ),
    )


def main(page: ft.Page):
    page.title = "告警视听助手"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 20
    page.bgcolor = "#121212"
    page.window_width = 1200
    page.window_height = 800

    tts = get_default_engine()
    tracker = AlarmTracker(announce_existing=False)
    poll_state = {"active": False, "interval_seconds": 30}
    event_log: list[str] = []
    ui_cache = {"last_history_alarms": []}

    def get_api_client() -> AlarmApiClient:
        return AlarmApiClient(input_host.value, input_token.value)

    def get_speech_rule_id() -> str:
        return input_speech_rule.value or RULE_AUTO

    # ---------- 左侧控件 ----------
    title_label = ft.Text(
        "数据流控制中枢", size=20, weight=ft.FontWeight.BOLD, color=ft.colors.WHITE,
    )

    # 默认值留空，避免把内网地址/Token 提交到 Git
    input_host = ft.TextField(
        label="服务器 HOST", value="",
        hint_text="例如 http://你的监控服务器地址",
        border_color="#444444", focused_border_color=ft.colors.BLUE_500,
        border_radius=4, prefix_icon=ft.icons.DNS,
        text_size=13, content_padding=10, dense=True,
    )
    input_token = ft.TextField(
        label="访问令牌 (Token)",
        value="",
        hint_text="在界面中填写，勿写入代码",
        password=True, can_reveal_password=True,
        border_color="#444444", focused_border_color=ft.colors.BLUE_500,
        border_radius=4, prefix_icon=ft.icons.VPN_KEY,
        text_size=13, content_padding=10, dense=True,
    )

    input_timer = ft.Dropdown(
        label="间隔",
        options=[
            ft.dropdown.Option("30", "30 秒"),
            ft.dropdown.Option("60", "1 分钟"),
        ],
        value="30",
        border_color="#444444", focused_border_color=ft.colors.BLUE_500,
        border_radius=4, text_size=13, dense=True,
    )

    input_speech_rule = ft.Dropdown(
        label="播报规则",
        options=[
            ft.dropdown.Option(rid, label) for rid, label in SPEECH_RULE_OPTIONS
        ],
        value=RULE_AUTO,
        border_color="#444444", focused_border_color=ft.colors.BLUE_500,
        border_radius=4, text_size=13, dense=True,
        prefix_icon=ft.icons.TUNE,
    )

    input_tts_backend = ft.Dropdown(
        label="语音引擎",
        options=[
            ft.dropdown.Option(BACKEND_LOCAL, "Windows SAPI 离线"),
            ft.dropdown.Option(BACKEND_XFYUN, "讯飞在线"),
        ],
        value=tts.get_backend(),
        border_color="#444444", focused_border_color=ft.colors.BLUE_500,
        border_radius=4, text_size=13, dense=True,
        prefix_icon=ft.icons.RECORD_VOICE_OVER,
    )
    tts_hint_text = ft.Text("", color="#ffb74d", size=11)

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
        return f"SAPI 语速: {rate} ({hint})"

    slider_local_rate = ft.Slider(
        min=-10, max=10, divisions=20,
        value=tts.get_local_rate(),
        active_color="#64b5f6",
        inactive_color="#333333",
        on_change=lambda e: on_local_rate_change(e),
    )
    local_rate_text = ft.Text(
        _local_rate_label(tts.get_local_rate()),
        size=11, color=ft.colors.GREY_500,
    )
    local_rate_panel = ft.Column(
        controls=[
            local_rate_text,
            slider_local_rate,
            ft.Text("← 慢          快 →", size=10, color="#666666"),
        ],
        spacing=4,
        visible=(tts.get_backend() == BACKEND_LOCAL),
    )

    def on_local_rate_change(e):
        rate = int(round(slider_local_rate.value))
        tts.set_local_rate(rate)
        local_rate_text.value = _local_rate_label(rate)
        page.update()

    switch_voice = ft.Switch(
        label="语音播报", value=True, active_color=ft.colors.GREEN_400,
        label_style=ft.TextStyle(size=13),
        on_change=lambda e: tts.set_enabled(switch_voice.value),
    )

    input_sapi_test = ft.TextField(
        label="SAPI 试听文字（可自定义）",
        value=build_port_table_speech_text("B05_NE20E-1", "up"),
        border_color="#444444", focused_border_color=ft.colors.BLUE_500,
        border_radius=4, text_size=13, content_padding=10, dense=True,
        multiline=True, min_lines=2, max_lines=3,
    )

    btn_test_sapi = ft.OutlinedButton(
        text="试听 SAPI", icon=ft.icons.HEARING,
        height=34,
        style=ft.ButtonStyle(
            color="#64b5f6",
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
            shape=ft.RoundedRectangleBorder(radius=4),
            text_style=ft.TextStyle(size=12, color="#64b5f6"),
            side=ft.BorderSide(1, "#1565c0"),
        ),
    )

    def on_click_test_sapi(e):
        raw = (input_sapi_test.value or "").strip()
        if not raw:
            console_output.value = "❌ 请在「SAPI 试听文字」中输入要播报的内容"
            page.update()
            return

        polished = polish_speech_for_tts(raw)
        rate = tts.get_local_rate()
        now = datetime.now().strftime("%H:%M:%S")

        out = f"🔈 [{now}] SAPI 离线试听\n{'=' * 70}\n"
        out += f"原始: {raw}\n"
        if polished != raw:
            out += f"润色: {polished}\n"
        out += f"语速: {rate} | 引擎: Windows SAPI（强制）\n"
        out += "-" * 70 + "\n推送中...\n"

        console_output.value = out
        set_status("SAPI 试听")
        page.update()

        tts.speak_local(polished)

        err = tts.last_error()
        out += f"⚠️ {err}\n" if err else "✅ 已推送 SAPI 播报\n"
        console_output.value = out
        page.update()

    btn_test_sapi.on_click = on_click_test_sapi

    def on_tts_backend_change(e):
        err = tts.set_backend(input_tts_backend.value)
        if err:
            tts_hint_text.value = f"⚠️ {err}"
            input_tts_backend.value = tts.get_backend()
        else:
            tts_hint_text.value = ""
        local_rate_panel.visible = (tts.get_backend() == BACKEND_LOCAL)
        tts_backend_text.value = f"TTS: {tts.backend_label()}"
        page.update()

    input_tts_backend.on_change = on_tts_backend_change

    status_text = ft.Text("待命", color=ft.colors.GREY_400, size=12)
    tts_backend_text = ft.Text(
        f"TTS: {tts.backend_label()}", color=ft.colors.GREY_600, size=11,
    )
    track_badge = ft.Container(
        content=ft.Text("追踪 0 条", size=11, color="#90caf9"),
        bgcolor="#1a237e22", border_radius=4,
        padding=ft.padding.symmetric(horizontal=8, vertical=4),
        border=ft.border.all(1, "#1e3a5f"),
    )

    # ---------- 右侧终端 ----------
    console_output = ft.TextField(
        multiline=True, read_only=True, expand=True,
        value=WELCOME_TEXT,
        text_size=13,
        text_style=ft.TextStyle(font_family="Consolas", color="#4CAF50"),
        border=ft.InputBorder.NONE, bgcolor="transparent",
    )

    def push_event_log(line: str) -> None:
        event_log.insert(0, line)
        del event_log[40:]

    def update_track_badge():
        track_badge.content.value = f"追踪 {tracker.tracking_count()} 条"
        track_badge.update()

    def render_polling_view(active_list, refresh_time: str) -> str:
        out = "📜 事件播报日志 (最新在上)\n"
        out += "=" * 70 + "\n"
        out += ("\n".join(event_log) if event_log else "（暂无新增 / 恢复事件）") + "\n"
        out += "=" * 70 + "\n\n"
        out += f"⚡ [{refresh_time}] 活跃告警: {len(active_list)} 条\n"
        out += "-" * 70 + "\n\n"
        for alarm in active_list:
            out += format_alarm_for_active_console(alarm) + ("-" * 70) + "\n\n"
        return out

    def set_status(msg: str):
        status_text.value = msg

    # ---------- 历史查询 ----------
    def on_click_history(e):
        client = get_api_client()
        if not client.host or not client.token:
            console_output.value = "❌ 错误: HOST 或 Token 不能为空！"
            page.update()
            return

        console_output.value = "⏳ 正在请求最近 100 条历史记录...\n"
        page.update()

        result = client.fetch_history(limit=100)
        if not result.ok:
            console_output.value = f"❌ {result.error}"
            page.update()
            return

        ui_cache["last_history_alarms"] = result.data
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out = f"✅ [{now}] 历史拉取成功 | 共 {len(result.data)} 条\n"
        out += "=" * 70 + "\n\n"
        for idx, alarm in enumerate(result.data, 1):
            out += format_alarm_for_console(alarm, idx=idx) + ("-" * 70) + "\n\n"

        console_output.value = out
        set_status("已拉取历史")
        page.update()

    # ---------- 轮询 ----------
    def poll_once():
        client = get_api_client()
        if not client.host or not client.token:
            return

        result = client.fetch_active_alarms(limit=50)
        if not result.ok:
            set_status(f"轮询失败: {result.error}")
            page.update()
            return

        events = tracker.diff(result.data, query_by_id=client.query_by_id)
        for ev in events:
            push_event_log(ev.log_line)
            speech = event_to_speech(ev, get_speech_rule_id())
            if speech:
                tts.speak(speech)

        refresh_time = datetime.now().strftime("%H:%M:%S")
        console_output.value = render_polling_view(result.data, refresh_time)
        set_status(f"轮询中 {poll_state['interval_seconds']}s | 刷新 {refresh_time}")
        update_track_badge()
        page.update()

    def on_click_poll_toggle(e):
        if poll_state["active"]:
            poll_state["active"] = False
            btn_poll.text = "开始"
            btn_poll.icon = ft.icons.PLAY_ARROW
            btn_poll.bgcolor = "#1565c0"
            input_timer.disabled = False
            set_status("待命")
        else:
            poll_state["interval_seconds"] = int(input_timer.value)
            poll_state["active"] = True
            btn_poll.text = "停止"
            btn_poll.icon = ft.icons.STOP
            btn_poll.bgcolor = "#c62828"
            input_timer.disabled = True
            set_status(f"轮询中 {poll_state['interval_seconds']}s")
            poll_once()
        btn_poll.update()
        input_timer.update()
        page.update()

    def background_polling_task():
        while True:
            if poll_state["active"]:
                for _ in range(poll_state["interval_seconds"]):
                    if not poll_state["active"]:
                        break
                    time.sleep(1)
                if poll_state["active"]:
                    poll_once()
            else:
                time.sleep(1)

    threading.Thread(target=background_polling_task, daemon=True).start()

    # ---------- 测试语音 ----------
    def on_click_test_speech(e):
        alarms = ui_cache["last_history_alarms"]
        if not alarms:
            client = get_api_client()
            if not client.host or not client.token:
                console_output.value = "❌ 请先填写 HOST 和 Token，或先拉取历史"
                page.update()
                return
            console_output.value = "⏳ 无缓存，正在拉取 1 条历史...\n"
            page.update()
            result = client.fetch_history(limit=1)
            if not result.ok or not result.data:
                console_output.value = f"❌ 无法获取测试数据: {result.error}"
                page.update()
                return
            alarms = result.data
            ui_cache["last_history_alarms"] = alarms

        alarm = alarms[0]
        rule_id = get_speech_rule_id()
        speech_text = alarm_to_speech(alarm, rule_id)
        device = alarm.get("instance_name", "未知设备")
        title = alarm.get("alarm_title", "未命名告警")
        status = "已恢复" if alarm.get("is_recover", 0) == 1 else "未处理"
        last2 = parse_last2_state(alarm) or "未解析"
        last2_cn = PORT_STATE_TTS_CN.get(last2, last2) if last2 != "未解析" else "—"
        now = datetime.now().strftime("%H:%M:%S")

        out = f"🔊 [{now}] 语音测试\n{'=' * 70}\n"
        out += f"来源: {device} | {title} | {status}\n"
        out += f"规则: {speech_rule_label(rule_id)} | last(2)={last2}→{last2_cn}\n"
        out += f"播报: 「{speech_text}」\n"
        out += f"引擎: {tts.backend_label()}\n{'-' * 70}\n"

        console_output.value = out + "推送中...\n"
        set_status("测试语音")
        page.update()

        tts.speak(speech_text)
        err = tts.last_error()
        out += f"⚠️ {err}\n" if err else "✅ 已推送\n"
        console_output.value = out
        page.update()

    def on_clear_console(e):
        console_output.value = WELCOME_TEXT
        page.update()

    # ---------- 按钮实例 ----------
    btn_history = _action_btn(
        "历史告警 (100条)", ft.icons.HISTORY, "#d84315", on_click_history,
    )
    btn_poll = _action_btn("开始", ft.icons.PLAY_ARROW, "#1565c0", on_click_poll_toggle)
    btn_test_speech = ft.OutlinedButton(
        text="测试语音", icon=ft.icons.RECORD_VOICE_OVER,
        on_click=on_click_test_speech, height=34,
        style=ft.ButtonStyle(
            color="#81c784",
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
            shape=ft.RoundedRectangleBorder(radius=4),
            text_style=ft.TextStyle(size=12, color="#81c784"),
            side=ft.BorderSide(1, "#2e7d32"),
        ),
    )

    # 轮询区: 间隔下拉 + 开始/停止 同一行
    poll_row = ft.Row(
        controls=[
            ft.Container(content=input_timer, expand=3),
            ft.Container(content=btn_poll, expand=2),
        ],
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.END,
    )

    # 状态底栏
    status_bar = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(
                    controls=[status_text, track_badge],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                tts_backend_text,
            ],
            spacing=4,
        ),
        bgcolor="#161616", border_radius=6,
        padding=ft.padding.all(10),
        border=ft.border.all(1, "#2a2a2a"),
    )

    left_panel = ft.Container(
        content=ft.Column(
            controls=[
                title_label,
                ft.Divider(height=4, color="transparent"),
                _section_title("连接配置"),
                input_host,
                input_token,
                ft.Divider(height=4, color="transparent"),
                _section_title("轮询监控"),
                poll_row,
                _section_title("语音设置"),
                input_speech_rule,
                input_tts_backend,
                local_rate_panel,
                input_sapi_test,
                btn_test_sapi,
                tts_hint_text,
                switch_voice,
                ft.Divider(height=4, color="transparent"),
                _section_title("手动操作"),
                btn_history,
                btn_test_speech,
                ft.Container(expand=True),
                status_bar,
            ],
            spacing=10,
            expand=True,
        ),
        bgcolor="#1e1e1e", padding=18, border_radius=8,
        border=ft.border.all(1, "#333333"), expand=1,
    )

    right_panel = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Icon(ft.icons.TERMINAL, color=ft.colors.GREY_400, size=18),
                        ft.Text("数据终端", color=ft.colors.GREY_400,
                                weight=ft.FontWeight.BOLD, size=14),
                        ft.Container(expand=True),
                        ft.IconButton(
                            icon=ft.icons.DELETE_SWEEP,
                            icon_size=18,
                            icon_color=ft.colors.GREY_600,
                            tooltip="清空终端",
                            on_click=on_clear_console,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.START,
                ),
                ft.Divider(color="#333333", height=1),
                console_output,
            ],
            expand=True, spacing=8,
        ),
        bgcolor="#0a0a0a", padding=16, border_radius=8,
        border=ft.border.all(1, "#333333"), expand=2,
    )

    page.add(ft.Row(controls=[left_panel, right_panel], expand=True, spacing=20))


if __name__ == "__main__":
    ft.app(target=main)
