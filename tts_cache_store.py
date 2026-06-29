"""
MOSS 语音缓存存储 — L1 片段 + L2 整句 + 热词表
================================================
管理 cache/ 目录下的 WAV 文件与 JSON 索引。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from tts_config import get_cache_settings, get_moss_settings, project_root
from tts_normalize import normalize_for_cache

# 热词状态常量
STATUS_PENDING = "pending"
STATUS_SYNTHESIZING = "synthesizing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

SOURCE_SYSTEM = "system"
SOURCE_HISTORY = "history"
SOURCE_MANUAL = "manual"
SOURCE_RUNTIME = "runtime"

_store_lock = threading.RLock()


def cache_root() -> Path:
    return project_root() / "cache"


def _fragment_dir(engine: str = "moss", voice: str = "Junhao") -> Path:
    return cache_root() / engine / voice


def _utterance_dir(engine: str = "moss", voice: str = "Junhao") -> Path:
    return cache_root() / "utterances" / engine / voice


def _safe_filename(text: str, max_len: int = 80) -> str:
    """将规范化文本转为安全文件名（保留中文）。"""
    name = re.sub(r'[\\/:*?"<>|]', "_", text.strip())
    name = name.replace("\n", "_").replace("\r", "")
    if len(name) > max_len:
        name = name[:max_len]
    return name or "empty"


def make_cache_key(text: str, engine: str = "moss", voice: str = "Junhao") -> str:
    """片段缓存 Key：hash(engine + voice + normalized_text)。"""
    norm = normalize_for_cache(text)
    raw = f"{engine}|{voice}|{norm}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def estimate_wav_size_kb(text: str) -> float:
    """粗估短词 WAV 大小（KB），用于导入前空间校验。"""
    n = max(1, len(normalize_for_cache(text)))
    return min(200.0, max(20.0, n * 8.0))


@dataclass
class CacheStats:
    """缓存存储统计。"""

    disk_used_bytes: int = 0
    disk_used_mb: float = 0.0
    entry_count: int = 0
    ready_count: int = 0
    max_disk_mb: int = 500
    max_entries: int = 500
    auto_lru_at_percent: int = 90

    @property
    def usage_percent(self) -> float:
        if self.max_disk_mb <= 0:
            return 0.0
        return min(100.0, self.disk_used_mb / self.max_disk_mb * 100)


class TtsCacheStore:
    """线程安全的缓存读写。"""

    def __init__(self) -> None:
        self._engine = "moss"
        self._voice = get_moss_settings().get("voice", "Junhao")
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        _fragment_dir(self._engine, self._voice).mkdir(parents=True, exist_ok=True)
        _utterance_dir(self._engine, self._voice).mkdir(parents=True, exist_ok=True)
        cache_root().mkdir(parents=True, exist_ok=True)

    def _index_path(self) -> Path:
        return cache_root() / "index.json"

    def _hotwords_path(self) -> Path:
        return cache_root() / "hotwords.json"

    def _stats_path(self) -> Path:
        return cache_root() / "stats.json"

    def _utterance_index_path(self) -> Path:
        return cache_root() / "utterance_index.json"

    def _warmup_job_path(self) -> Path:
        return cache_root() / "warmup_job.json"

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.is_file():
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)

    def _load_index(self) -> dict:
        data = self._read_json(self._index_path(), {"version": 1, "entries": {}})
        if "entries" not in data:
            data["entries"] = {}
        return data

    def _save_index(self, data: dict) -> None:
        self._write_json(self._index_path(), data)

    def _load_hotwords(self) -> dict:
        data = self._read_json(
            self._hotwords_path(),
            {"version": 1, "entries": []},
        )
        if "entries" not in data:
            data["entries"] = []
        return data

    def _save_hotwords(self, data: dict) -> None:
        self._write_json(self._hotwords_path(), data)

    def _load_utterance_index(self) -> dict:
        data = self._read_json(self._utterance_index_path(), {"version": 1, "entries": {}})
        if "entries" not in data:
            data["entries"] = {}
        return data

    def _save_utterance_index(self, data: dict) -> None:
        self._write_json(self._utterance_index_path(), data)

    def fragment_path_for_text(self, text: str) -> Path:
        """片段 WAV 磁盘路径。"""
        norm = normalize_for_cache(text)
        fname = _safe_filename(norm) + ".wav"
        return _fragment_dir(self._engine, self._voice) / fname

    def get_fragment(self, text: str) -> Optional[bytes]:
        """读取 L1 片段 WAV；不存在返回 None。"""
        with _store_lock:
            key = make_cache_key(text, self._engine, self._voice)
            idx = self._load_index()
            entry = idx["entries"].get(key)
            if not entry or entry.get("status") != STATUS_READY:
                return None
            path = Path(entry.get("path", ""))
            if not path.is_file():
                path = self.fragment_path_for_text(text)
            if path.is_file():
                return path.read_bytes()
            return None

    def put_fragment(
        self,
        text: str,
        wav_bytes: bytes,
        *,
        source: str = SOURCE_RUNTIME,
        slot_type: str = "",
        pinned: bool = False,
    ) -> str:
        """写入 L1 片段并更新索引与热词表。返回 cache_key。"""
        with _store_lock:
            norm = normalize_for_cache(text)
            key = make_cache_key(norm, self._engine, self._voice)
            path = self.fragment_path_for_text(norm)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(wav_bytes)
            size = len(wav_bytes)
            now = datetime.now().isoformat(timespec="seconds")

            idx = self._load_index()
            old = idx["entries"].get(key, {})
            idx["entries"][key] = {
                "text": norm,
                "path": str(path.relative_to(project_root())).replace("\\", "/"),
                "engine": self._engine,
                "voice": self._voice,
                "status": STATUS_READY,
                "file_size": size,
                "hit_count": old.get("hit_count", 0),
                "source": source,
                "slot_type": slot_type,
                "pinned": pinned or old.get("pinned", False),
                "created_at": old.get("created_at", now),
                "last_used_at": now,
            }
            self._save_index(idx)
            self._upsert_hotword_locked(
                norm,
                source=source,
                status=STATUS_READY,
                file_size=size,
                slot_type=slot_type,
                pinned=pinned,
            )
            self.refresh_stats()
            return key

    def touch_hit(self, text: str) -> None:
        """片段命中计数 +1。"""
        with _store_lock:
            key = make_cache_key(text, self._engine, self._voice)
            idx = self._load_index()
            entry = idx["entries"].get(key)
            if not entry:
                return
            entry["hit_count"] = int(entry.get("hit_count", 0)) + 1
            entry["last_used_at"] = datetime.now().isoformat(timespec="seconds")
            self._save_index(idx)
            hw = self._load_hotwords()
            for e in hw["entries"]:
                if normalize_for_cache(e.get("text", "")) == normalize_for_cache(text):
                    e["hit_count"] = int(e.get("hit_count", 0)) + 1
                    break
            self._save_hotwords(hw)

    def has_utterance(self, alarm_id: Any, text_hash: str) -> bool:
        """检查 L2 整句是否已存在（不增加播放计数）。"""
        with _store_lock:
            ukey = f"alarm_{alarm_id}_{text_hash}"
            uidx = self._load_utterance_index()
            entry = uidx["entries"].get(ukey)
            if not entry:
                return False
            path = project_root() / entry.get("path", "")
            return path.is_file()

    def get_utterance(self, alarm_id: Any, text_hash: str) -> Optional[bytes]:
        """读取 L2 整句 WAV。"""
        with _store_lock:
            ukey = f"alarm_{alarm_id}_{text_hash}"
            uidx = self._load_utterance_index()
            entry = uidx["entries"].get(ukey)
            if not entry:
                return None
            path = project_root() / entry.get("path", "")
            if path.is_file():
                entry["play_count"] = int(entry.get("play_count", 0)) + 1
                entry["last_played_at"] = datetime.now().isoformat(timespec="seconds")
                self._save_utterance_index(uidx)
                return path.read_bytes()
            return None

    def put_utterance(
        self,
        alarm_id: Any,
        text: str,
        text_hash: str,
        wav_bytes: bytes,
        *,
        first_play_mode: str = "stitch",
    ) -> None:
        """写入 L2 整句缓存。"""
        with _store_lock:
            ukey = f"alarm_{alarm_id}_{text_hash}"
            fname = f"alarm_{alarm_id}_{text_hash}.wav"
            path = _utterance_dir(self._engine, self._voice) / fname
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(wav_bytes)
            rel = str(path.relative_to(project_root())).replace("\\", "/")
            uidx = self._load_utterance_index()
            uidx["entries"][ukey] = {
                "alarm_id": alarm_id,
                "text": text,
                "text_hash": text_hash,
                "path": rel,
                "play_count": 0,
                "first_play_mode": first_play_mode,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "last_played_at": "",
            }
            self._save_utterance_index(uidx)
            self.refresh_stats()

    def list_hotwords(self, status_filter: str = "all") -> list[dict]:
        """列出热词表条目。"""
        with _store_lock:
            hw = self._load_hotwords()
            entries = list(hw.get("entries") or [])
            if status_filter == "all":
                return entries
            return [e for e in entries if e.get("status") == status_filter]

    def _upsert_hotword_locked(
        self,
        text: str,
        *,
        source: str = SOURCE_MANUAL,
        status: str = STATUS_PENDING,
        file_size: int = 0,
        slot_type: str = "",
        pinned: bool = False,
        history_count: int = 0,
    ) -> dict:
        norm = normalize_for_cache(text)
        hw = self._load_hotwords()
        entries = hw["entries"]
        for e in entries:
            if normalize_for_cache(e.get("text", "")) == norm:
                e["status"] = status
                if file_size:
                    e["file_size"] = file_size
                if history_count:
                    e["history_count"] = int(e.get("history_count", 0)) + history_count
                if pinned:
                    e["pinned"] = True
                if slot_type:
                    e["slot_type"] = slot_type
                if source == SOURCE_MANUAL:
                    e["source"] = SOURCE_MANUAL
                e["updated_at"] = datetime.now().isoformat(timespec="seconds")
                self._save_hotwords(hw)
                return e

        entry = {
            "text": norm,
            "source": source,
            "status": status,
            "slot_type": slot_type,
            "file_size": file_size,
            "hit_count": 0,
            "history_count": history_count,
            "pinned": pinned,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        entries.append(entry)
        self._save_hotwords(hw)
        return entry

    def upsert_hotword(
        self,
        text: str,
        *,
        source: str = SOURCE_MANUAL,
        status: str = STATUS_PENDING,
        pinned: bool = False,
        history_count: int = 0,
        slot_type: str = "",
    ) -> dict:
        with _store_lock:
            return self._upsert_hotword_locked(
                text,
                source=source,
                status=status,
                pinned=pinned,
                history_count=history_count,
                slot_type=slot_type,
            )

    def update_hotword_status(self, text: str, status: str, *, error: str = "") -> None:
        with _store_lock:
            norm = normalize_for_cache(text)
            hw = self._load_hotwords()
            for e in hw["entries"]:
                if normalize_for_cache(e.get("text", "")) == norm:
                    e["status"] = status
                    e["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    if error:
                        e["last_error"] = error
                    break
            self._save_hotwords(hw)

    def delete_hotword(self, text: str, *, delete_wav: bool = True) -> None:
        with _store_lock:
            norm = normalize_for_cache(text)
            key = make_cache_key(norm, self._engine, self._voice)
            hw = self._load_hotwords()
            hw["entries"] = [
                e for e in hw["entries"]
                if normalize_for_cache(e.get("text", "")) != norm
            ]
            self._save_hotwords(hw)
            idx = self._load_index()
            entry = idx["entries"].pop(key, None)
            self._save_index(idx)
            if delete_wav and entry:
                p = project_root() / entry.get("path", "")
                if p.is_file():
                    p.unlink(missing_ok=True)
            self.refresh_stats()

    def merge_hotwords(self, new_entries: list[dict]) -> int:
        """合并热词（历史分析导入）。返回新增条数。"""
        added = 0
        with _store_lock:
            for item in new_entries:
                text = item.get("text", "")
                if not text:
                    continue
                norm = normalize_for_cache(text)
                hw = self._load_hotwords()
                exists = any(
                    normalize_for_cache(e.get("text", "")) == norm
                    for e in hw["entries"]
                )
                if exists:
                    for e in hw["entries"]:
                        if normalize_for_cache(e.get("text", "")) == norm:
                            e["history_count"] = int(e.get("history_count", 0)) + int(
                                item.get("history_count", 1)
                            )
                    self._save_hotwords(hw)
                    continue
                self._upsert_hotword_locked(
                    norm,
                    source=item.get("source", SOURCE_HISTORY),
                    status=STATUS_PENDING,
                    slot_type=item.get("slot_type", ""),
                    history_count=int(item.get("history_count", 1)),
                )
                added += 1
        self.refresh_stats()
        return added

    def get_stats(self) -> CacheStats:
        with _store_lock:
            cfg = get_cache_settings()
            data = self._read_json(self._stats_path(), {})
            return CacheStats(
                disk_used_bytes=int(data.get("disk_used_bytes", 0)),
                disk_used_mb=float(data.get("disk_used_mb", 0)),
                entry_count=int(data.get("entry_count", 0)),
                ready_count=int(data.get("ready_count", 0)),
                max_disk_mb=int(cfg.get("max_disk_mb", 500)),
                max_entries=int(cfg.get("max_entries", 500)),
                auto_lru_at_percent=int(cfg.get("auto_lru_at_percent", 90)),
            )

    def refresh_stats(self) -> CacheStats:
        with _store_lock:
            cfg = get_cache_settings()
            idx = self._load_index()
            uidx = self._load_utterance_index()
            total = 0
            ready = 0
            for e in idx.get("entries", {}).values():
                total += int(e.get("file_size", 0))
                if e.get("status") == STATUS_READY:
                    ready += 1
            for e in uidx.get("entries", {}).values():
                p = project_root() / e.get("path", "")
                if p.is_file():
                    total += p.stat().st_size
            # 索引文件本身
            for p in [self._index_path(), self._hotwords_path(), self._utterance_index_path()]:
                if p.is_file():
                    total += p.stat().st_size

            stats = {
                "disk_used_bytes": total,
                "disk_used_mb": round(total / (1024 * 1024), 2),
                "entry_count": len(idx.get("entries", {})),
                "ready_count": ready,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._write_json(self._stats_path(), stats)
            return CacheStats(
                disk_used_bytes=total,
                disk_used_mb=stats["disk_used_mb"],
                entry_count=stats["entry_count"],
                ready_count=ready,
                max_disk_mb=int(cfg.get("max_disk_mb", 500)),
                max_entries=int(cfg.get("max_entries", 500)),
                auto_lru_at_percent=int(cfg.get("auto_lru_at_percent", 90)),
            )

    def can_import_hotwords(self, new_entries: list[dict]) -> tuple[bool, str]:
        stats = self.get_stats()
        estimated_mb = sum(estimate_wav_size_kb(e.get("text", "")) for e in new_entries) / 1024
        if stats.disk_used_mb + estimated_mb > stats.max_disk_mb * 0.95:
            return False, "剩余空间不足，请清理或提高上限"
        if stats.entry_count + len(new_entries) > stats.max_entries:
            return False, f"条目数将超过上限 {stats.max_entries}"
        return True, ""

    def run_lru_if_needed(self) -> int:
        """磁盘使用超阈值时 LRU 淘汰。返回删除条数。"""
        with _store_lock:
            stats = self.refresh_stats()
            threshold = stats.max_disk_mb * stats.auto_lru_at_percent / 100
            if stats.disk_used_mb < threshold:
                return 0

            idx = self._load_index()
            candidates = []
            for key, e in idx["entries"].items():
                if e.get("source") == SOURCE_SYSTEM:
                    continue
                if e.get("pinned"):
                    continue
                candidates.append((key, e))

            candidates.sort(
                key=lambda x: (int(x[1].get("hit_count", 0)), x[1].get("last_used_at", "")),
            )
            removed = 0
            for key, e in candidates:
                stats = self.refresh_stats()
                if stats.disk_used_mb < threshold * 0.85:
                    break
                path = project_root() / e.get("path", "")
                if path.is_file():
                    path.unlink(missing_ok=True)
                del idx["entries"][key]
                self.delete_hotword(e.get("text", ""), delete_wav=False)
                removed += 1
            self._save_index(idx)
            self.refresh_stats()
            return removed

    def get_warmup_job(self) -> dict:
        return self._read_json(
            self._warmup_job_path(),
            {"running": False, "total": 0, "done": 0, "current": "", "failed": 0},
        )

    def set_warmup_job(self, data: dict) -> None:
        self._write_json(self._warmup_job_path(), data)

    def mark_all_resynth(self, exclude_system: bool = True) -> int:
        """符号规则变更后：非 system 条目标为 pending。"""
        count = 0
        with _store_lock:
            hw = self._load_hotwords()
            for e in hw["entries"]:
                if exclude_system and e.get("source") == SOURCE_SYSTEM:
                    continue
                if e.get("status") == STATUS_READY:
                    e["status"] = STATUS_PENDING
                    count += 1
            self._save_hotwords(hw)
        return count


_default_store: Optional[TtsCacheStore] = None


def get_cache_store() -> TtsCacheStore:
    global _default_store
    if _default_store is None:
        _default_store = TtsCacheStore()
    return _default_store
