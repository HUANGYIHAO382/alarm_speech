"""
MOSS 预缓存面板 UI — 五大功能
==============================
供 flet_demo 右栏下半部分调用。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable, Optional

import flet as ft

from tts_cache_store import (
    SOURCE_MANUAL,
    SOURCE_SYSTEM,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_SYNTHESIZING,
    get_cache_store,
)
from tts_config import get_cache_settings
from tts_history_analyzer import HistoryHotwordAnalyzer
from tts_normalize import load_symbol_rules, preview_normalize, save_symbol_rules
from tts_warmup_job import WarmupJob

# 与 flet_demo 设计令牌对齐
C_BORDER = "#2a3140"
C_ACCENT = "#5b9cf5"
C_SUCCESS = "#6ecf8a"
C_WARN = "#f0b45c"
C_DANGER = "#e85d5d"
C_TEXT = "#e8eaed"
C_TEXT_MUTED = "#8b93a7"
C_SURFACE_ALT = "#1f2530"
CTRL_TEXT_SIZE = 12

STATUS_LABELS = {
    STATUS_PENDING: ("待制作", C_TEXT_MUTED),
    STATUS_SYNTHESIZING: ("制作中", C_WARN),
    STATUS_READY: ("已制作", C_SUCCESS),
    STATUS_FAILED: ("失败", C_DANGER),
}

SOURCE_LABELS = {
    SOURCE_SYSTEM: "系统",
    "history": "历史",
    SOURCE_MANUAL: "手动",
    "runtime": "运行时",
}


def build_moss_cache_panel(
    page: ft.Page,
    *,
    tts_engine,
    append_console_line: Callable[[str], None],
    get_speech_rule_id: Callable[[], str],
    ui_cache: dict,
    get_api_client: Callable,
    is_moss_backend: Callable[[], bool],
) -> ft.Container:
    """
    构建 MOSS 预缓存工作台面板。
    返回 Container，内含 refresh 方法挂载在 .data["refresh"]。
    """
    store = get_cache_store()
    warmup = WarmupJob.instance()
    warmup.set_synthesize_fn(tts_engine.synthesize_moss_bytes)

    # ---------- 存储监控条 ----------
    storage_bar = ft.ProgressBar(value=0, bar_height=8, color=C_ACCENT, bgcolor=C_BORDER)
    storage_text = ft.Text("0 / 500 MB", size=11, color=C_TEXT)
    storage_detail = ft.Text("条目 0 · 已制作 0", size=10, color=C_TEXT_MUTED)

    # ---------- 热词筛选（用 Dropdown 代替 Tabs，规避 Flet 0.25 + Py3.8 的 Tabs 崩溃）----------
    filter_dropdown = ft.Dropdown(
        label="筛选",
        value="all",
        width=140,
        dense=True,
        text_size=CTRL_TEXT_SIZE,
        border_color=C_BORDER,
        bgcolor=C_SURFACE_ALT,
        color=C_TEXT,
        options=[
            ft.dropdown.Option("all", "全部"),
            ft.dropdown.Option(STATUS_READY, "已制作"),
            ft.dropdown.Option(STATUS_PENDING, "待制作"),
            ft.dropdown.Option(STATUS_FAILED, "失败"),
        ],
    )
    hotword_list = ft.ListView(expand=True, spacing=2, padding=4, auto_scroll=False)

    # ---------- Warmup 进度 ----------
    warmup_bar = ft.ProgressBar(value=0, bar_height=4, color=C_SUCCESS, bgcolor=C_BORDER, visible=False)
    warmup_text = ft.Text("", size=10, color=C_TEXT_MUTED, visible=False)

    # ---------- 手动添加 ----------
    input_manual_word = ft.TextField(
        label="手动添加热词",
        hint_text="如 NE20E杠1 或 NE20E-1",
        text_size=CTRL_TEXT_SIZE,
        border_color=C_BORDER,
        bgcolor=C_SURFACE_ALT,
        color=C_TEXT,
        expand=True,
    )

    def _storage_bar_color(pct: float) -> str:
        if pct >= 90:
            return C_DANGER
        if pct >= 70:
            return C_WARN
        return C_ACCENT

    def _format_size_kb(size: int) -> str:
        if size <= 0:
            return "—"
        if size < 1024:
            return f"{size} B"
        return f"{size // 1024} KB"

    def _current_filter() -> str:
        return filter_dropdown.value or "all"

    def _make_hotword_row(entry: dict) -> ft.Container:
        """热词表单行。"""
        text = entry.get("text", "")
        status = entry.get("status", STATUS_PENDING)
        label, color = STATUS_LABELS.get(status, ("?", C_TEXT_MUTED))
        source = SOURCE_LABELS.get(entry.get("source", ""), entry.get("source", ""))

        def on_synth(_e):
            warmup.enqueue_single(text)

        def on_preview(_e):
            def _w():
                try:
                    tts_engine.speak_preview_blocking(text)
                except Exception:
                    pass
                page.update()

            threading.Thread(target=_w, daemon=True).start()

        def on_delete(_e):
            store.delete_hotword(text)
            refresh_panel()

        actions = []
        if status in (STATUS_PENDING, STATUS_FAILED):
            actions.append(ft.TextButton("合成", on_click=on_synth))
        if status == STATUS_READY:
            actions.append(ft.TextButton("试听", on_click=on_preview))
        actions.append(ft.TextButton("删除", on_click=on_delete, style=ft.ButtonStyle(color=C_DANGER)))

        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Text(text, size=11, color=C_TEXT, expand=True, no_wrap=True),
                    ft.Text(source, size=10, color=C_TEXT_MUTED, width=40),
                    ft.Container(
                        content=ft.Text(label, size=10, color=color),
                        width=52,
                    ),
                    ft.Text(_format_size_kb(entry.get("file_size", 0)), size=10, color=C_TEXT_MUTED, width=48),
                    ft.Text(str(entry.get("hit_count", 0)), size=10, color=C_TEXT_MUTED, width=32),
                    ft.Row(controls=actions, spacing=0),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(horizontal=6, vertical=4),
            border=ft.border.only(bottom=ft.BorderSide(1, C_BORDER)),
        )

    def refresh_panel() -> None:
        """刷新存储条与热词列表。"""
        stats = store.refresh_stats()
        pct = stats.usage_percent / 100.0
        storage_bar.value = min(1.0, pct)
        storage_bar.color = _storage_bar_color(stats.usage_percent)
        storage_text.value = f"{stats.disk_used_mb:.1f} / {stats.max_disk_mb} MB ({stats.usage_percent:.0f}%)"
        storage_detail.value = f"条目 {stats.entry_count} · 已制作 {stats.ready_count}"

        filt = _current_filter()
        entries = store.list_hotwords(filt)
        hotword_list.controls.clear()
        for e in entries[:100]:
            hotword_list.controls.append(_make_hotword_row(e))

        job = store.get_warmup_job()
        if job.get("running"):
            warmup_bar.visible = True
            warmup_text.visible = True
            total = max(1, int(job.get("total", 1)))
            done = int(job.get("done", 0))
            warmup_bar.value = done / total
            warmup_text.value = f"预合成 {done}/{total} · 当前: {job.get('current', '')}"
        else:
            warmup_bar.visible = False
            warmup_text.visible = False

        try:
            page.update()
        except Exception:
            pass

    def on_filter_change(_e):
        refresh_panel()

    filter_dropdown.on_change = on_filter_change

    # ---------- 从历史生成（三步弹窗）----------
    history_step = {"data": None}
    dlg_limit = ft.Dropdown(
        label="拉取条数",
        options=[
            ft.dropdown.Option("100"),
            ft.dropdown.Option("500"),
            ft.dropdown.Option("1000"),
            ft.dropdown.Option("2000"),
        ],
        value=str(get_cache_settings().get("history_fetch_default", 500)),
        width=200,
    )
    dlg_auto_synth = ft.Checkbox(label="分析后自动开始预合成", value=False)
    dlg_skip_ready = ft.Checkbox(label="跳过已制作条目", value=True)
    dlg_preview_text = ft.Text("", size=11, color=C_TEXT)

    def _close_dlg(dlg: ft.AlertDialog):
        dlg.open = False
        page.update()

    def on_history_analyze(_e):
        """步骤 2：分析历史。"""
        limit = int(dlg_limit.value or "500")
        alarms = ui_cache.get("last_history_alarms") or []
        if len(alarms) < limit:
            client = get_api_client()
            if client.host and client.token:
                append_console_line(f"[缓存] 正在拉取历史 {limit} 条…")
                result = client.fetch_history(limit=limit)
                if result.ok:
                    alarms = result.data
                    ui_cache["last_history_alarms"] = alarms

        analyzer = HistoryHotwordAnalyzer()
        analysis = analyzer.analyze_alarms(
            alarms,
            get_speech_rule_id(),
            skip_existing_ready=dlg_skip_ready.value,
        )
        history_step["data"] = analysis
        pt = analysis.preview_by_type
        dlg_preview_text.value = (
            f"共分析 {analysis.total_alarms} 条 → 新增 {len(analysis.new_entries)} 个片段\n"
            f"机柜 ×{pt.get('cabinet', 0)}  设备 ×{pt.get('device', 0)}\n"
            f"预计 WAV 约 {analysis.estimated_mb} MB · 合成约 {analysis.estimated_minutes} 分钟\n"
            f"示例: {', '.join(analysis.preview_samples[:5])}"
        )
        ok, reason = store.can_import_hotwords(analysis.new_entries)
        btn_import.disabled = not ok
        if not ok:
            dlg_preview_text.value += f"\n⚠ {reason}"
        page.update()

    def on_history_import(_e, dlg: ft.AlertDialog):
        analysis = history_step.get("data")
        if not analysis:
            return
        added = store.merge_hotwords(analysis.new_entries)
        append_console_line(f"[缓存] 历史分析完成，新增 {added} 条热词")
        if dlg_auto_synth.value:
            n = warmup.enqueue_pending_all()
            append_console_line(f"[缓存] 已加入预合成队列 {n} 条")
        _close_dlg(dlg)
        refresh_panel()

    btn_import = ft.ElevatedButton("确认并导入", disabled=True)

    def open_history_wizard(_e):
        history_step["data"] = None
        dlg_preview_text.value = ""
        btn_import.disabled = True

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("从历史生成热词"),
            content=ft.Column(
                controls=[
                    dlg_limit,
                    dlg_auto_synth,
                    dlg_skip_ready,
                    ft.ElevatedButton("分析", on_click=on_history_analyze),
                    dlg_preview_text,
                ],
                tight=True,
                spacing=10,
                width=420,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda e: _close_dlg(dlg)),
                btn_import,
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        btn_import.on_click = lambda e: on_history_import(e, dlg)
        page.overlay.append(dlg)
        dlg.open = True
        page.update()

    def on_warmup_all(_e):
        n = warmup.enqueue_pending_all()
        append_console_line(f"[缓存] 预合成全部待制作: {n} 条")
        refresh_panel()

    def on_warmup_fixed(_e):
        n = warmup.enqueue_fixed_tier0()
        append_console_line(f"[缓存] 固定词预热: 加入队列 {n} 条")
        refresh_panel()

    def on_manual_add(_e, synth: bool = False):
        raw = (input_manual_word.value or "").strip()
        if not raw:
            return
        norm, steps = preview_normalize(raw)
        store.upsert_hotword(norm, source=SOURCE_MANUAL, status=STATUS_PENDING)
        append_console_line(f"[缓存] 手动添加: {norm}")
        input_manual_word.value = ""
        if synth:
            warmup.enqueue_single(norm)
        refresh_panel()

    # ---------- 符号读法弹窗 ----------
    def open_symbol_rules_dlg(_e):
        rules = load_symbol_rules()
        preview_input = ft.TextField(label="预览输入", value="NE20E-1", width=300)
        preview_output = ft.Text("", size=11, color=C_TEXT)

        char_fields = []
        for cr in rules.char_rules:
            en = ft.Checkbox(label=f"{cr.get('symbol')} → {cr.get('reading')}", value=cr.get("enabled", True))
            char_fields.append((cr, en))

        def do_preview(_e):
            r = load_symbol_rules()
            for cr, cb in char_fields:
                cr["enabled"] = cb.value
            out, steps = preview_normalize(preview_input.value or "", r)
            preview_output.value = f"结果: {out}\n" + "\n".join(steps)

        def do_save(_e):
            r = load_symbol_rules()
            for cr, cb in char_fields:
                cr["enabled"] = cb.value
            save_symbol_rules(r)
            append_console_line("[缓存] 符号读法已保存")
            _close_dlg(dlg)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("符号读法设置"),
            content=ft.Column(
                controls=[
                    *[cb for _, cb in char_fields],
                    preview_input,
                    ft.TextButton("预览", on_click=do_preview),
                    preview_output,
                ],
                tight=True,
                spacing=8,
                width=400,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda e: _close_dlg(dlg)),
                ft.ElevatedButton("保存", on_click=do_save),
            ],
        )
        page.overlay.append(dlg)
        dlg.open = True
        page.update()

    warmup.set_progress_callback(lambda _s: refresh_panel())

    panel_body = ft.Column(
        controls=[
            ft.Text("MOSS 预缓存", size=13, weight=ft.FontWeight.W_600, color=C_TEXT),
            storage_text,
            storage_bar,
            storage_detail,
            ft.Row(
                controls=[
                    ft.ElevatedButton("从历史生成", icon=ft.Icons.HISTORY, on_click=open_history_wizard),
                    ft.OutlinedButton("预合成待制作", on_click=on_warmup_all),
                    ft.OutlinedButton("预热固定词", on_click=on_warmup_fixed),
                    ft.IconButton(icon=ft.Icons.SETTINGS, tooltip="符号读法", on_click=open_symbol_rules_dlg),
                ],
                wrap=True,
                spacing=6,
            ),
            warmup_text,
            warmup_bar,
            filter_dropdown,
            ft.Container(content=hotword_list, height=160, border=ft.border.all(1, C_BORDER), border_radius=6),
            ft.Row(
                controls=[
                    input_manual_word,
                    ft.ElevatedButton("添加", on_click=lambda e: on_manual_add(e, False)),
                    ft.ElevatedButton("添加并合成", on_click=lambda e: on_manual_add(e, True)),
                ],
                spacing=6,
            ),
        ],
        spacing=8,
    )

    container = ft.Container(
        content=panel_body,
        visible=is_moss_backend(),
        padding=ft.padding.only(top=8),
    )
    container.data = {"refresh": refresh_panel}
    refresh_panel()
    return container
