"""
MOSS 预热任务 — 批量/单条预合成 WAV
====================================
后台线程串行调用 moss_daemon，与实时播报共用守护进程。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from tts_cache_store import (
    SOURCE_MANUAL,
    SOURCE_SYSTEM,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_SYNTHESIZING,
    get_cache_store,
)
from tts_config import project_root
from tts_normalize import normalize_for_cache

# 优先级：数值越小越优先
PRIORITY_HIGH = 0
PRIORITY_NORMAL = 5
PRIORITY_LOW = 10


@dataclass(order=True)
class WarmupTask:
    """预热队列任务。"""

    priority: int
    text: str = field(compare=False)
    source: str = field(compare=False, default=SOURCE_MANUAL)


class WarmupJob:
    """单例后台预热任务管理器。"""

    _instance: Optional["WarmupJob"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._queue: queue.PriorityQueue[WarmupTask] = queue.PriorityQueue()
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._stop_flag = False
        self._synth_fn: Optional[Callable[[str], bytes]] = None
        self._progress_cb: Optional[Callable[[dict], None]] = None
        self._job_state = {
            "running": False,
            "total": 0,
            "done": 0,
            "failed": 0,
            "current": "",
        }

    @classmethod
    def instance(cls) -> "WarmupJob":
        with cls._lock:
            if cls._instance is None:
                cls._instance = WarmupJob()
            return cls._instance

    def set_synthesize_fn(self, fn: Callable[[str], bytes]) -> None:
        """注入 MOSS 合成函数（由 tts_engine 提供）。"""
        self._synth_fn = fn

    def set_progress_callback(self, cb: Callable[[dict], None]) -> None:
        self._progress_cb = cb

    def _emit_progress(self) -> None:
        store = get_cache_store()
        store.set_warmup_job(dict(self._job_state))
        if self._progress_cb:
            self._progress_cb(dict(self._job_state))

    def enqueue(self, text: str, *, source: str = SOURCE_MANUAL, priority: int = PRIORITY_NORMAL) -> None:
        norm = normalize_for_cache(text)
        store = get_cache_store()
        store.upsert_hotword(norm, source=source, status=STATUS_PENDING)
        self._queue.put(WarmupTask(priority=priority, text=norm, source=source))
        self._job_state["total"] = self._queue.qsize() + self._job_state["done"]
        self._ensure_worker()

    def enqueue_single(self, text: str, *, priority: int = PRIORITY_HIGH) -> None:
        self.enqueue(text, source=SOURCE_MANUAL, priority=priority)

    def enqueue_pending_all(self) -> int:
        """将热词表中所有 pending 条目加入队列。"""
        store = get_cache_store()
        count = 0
        for e in store.list_hotwords(STATUS_PENDING):
            self.enqueue(e.get("text", ""), source=e.get("source", SOURCE_MANUAL))
            count += 1
        return count

    def enqueue_fixed_tier0(self) -> int:
        """读取 assets/tts_fixed_phrases.json 并加入队列。"""
        path = project_root() / "assets" / "tts_fixed_phrases.json"
        if not path.is_file():
            return 0
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        count = 0
        for item in data.get("phrases") or []:
            text = item.get("text", "")
            if text:
                store = get_cache_store()
                # 已 ready 的跳过
                existing = store.get_fragment(text)
                if existing:
                    store.upsert_hotword(
                        text,
                        source=SOURCE_SYSTEM,
                        status=STATUS_READY,
                        slot_type=item.get("slot_type", ""),
                    )
                    continue
                self.enqueue(text, source=SOURCE_SYSTEM, priority=PRIORITY_LOW)
                count += 1
        return count

    def _ensure_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop_flag = False
        self._worker = threading.Thread(target=self._run, daemon=True, name="warmup-job")
        self._worker.start()

    def stop(self) -> None:
        self._stop_flag = True

    def _run(self) -> None:
        store = get_cache_store()
        self._running = True
        self._job_state["running"] = True
        self._emit_progress()

        while not self._stop_flag:
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                if self._queue.empty():
                    self._job_state["running"] = False
                    self._job_state["current"] = ""
                    self._emit_progress()
                continue

            text = task.text
            self._job_state["current"] = text
            self._emit_progress()
            store.update_hotword_status(text, STATUS_SYNTHESIZING)

            try:
                if self._synth_fn is None:
                    raise RuntimeError("MOSS 合成函数未注入")
                wav = self._synth_fn(text)
                store.put_fragment(text, wav, source=task.source)
                store.update_hotword_status(text, STATUS_READY)
                self._job_state["done"] += 1
            except Exception as err:
                store.update_hotword_status(text, STATUS_FAILED, error=str(err))
                self._job_state["failed"] += 1
            finally:
                self._emit_progress()
                store.run_lru_if_needed()

        self._running = False
        self._job_state["running"] = False
        self._emit_progress()

    def get_state(self) -> dict:
        return dict(self._job_state)
