"""
文本规范化模块 — 缓存 Key 与 MOSS 合成输入的统一入口
======================================================
将原始文本按 symbol_rules.json 规则规范化，保证：
  - 缓存 Key 一致
  - 热词表「词组」列与磁盘 WAV 文件名一致
  - 与 alarm_processor 播报润色逻辑对齐
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from tts_config import project_root


@dataclass
class SymbolRules:
    """符号读法规则（对应 cache/symbol_rules.json）。"""

    version: int = 1
    enabled: bool = True
    char_rules: list[dict] = field(default_factory=list)
    word_rules: list[dict] = field(default_factory=list)
    device_patterns: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "SymbolRules":
        return cls(
            version=int(data.get("version", 1)),
            enabled=bool(data.get("enabled", True)),
            char_rules=list(data.get("char_rules") or []),
            word_rules=list(data.get("word_rules") or []),
            device_patterns=list(data.get("device_patterns") or []),
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "char_rules": self.char_rules,
            "word_rules": self.word_rules,
            "device_patterns": self.device_patterns,
        }


def default_symbol_rules_path() -> Path:
    return project_root() / "cache" / "symbol_rules.json"


def load_symbol_rules(path: Optional[Path] = None) -> SymbolRules:
    """从磁盘加载符号规则；文件不存在时使用内置默认。"""
    p = path or default_symbol_rules_path()
    if not p.is_file():
        return SymbolRules()
    try:
        with open(p, "r", encoding="utf-8") as f:
            return SymbolRules.from_dict(json.load(f))
    except Exception:
        return SymbolRules()


def save_symbol_rules(rules: SymbolRules, path: Optional[Path] = None) -> None:
    """保存符号规则到磁盘。"""
    p = path or default_symbol_rules_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(rules.to_dict(), f, ensure_ascii=False, indent=2)


def _apply_device_patterns(text: str, rules: SymbolRules, steps: list[str]) -> str:
    """设备名模式：B05_NE20E-1 → B05柜，NE20E-1"""
    if not rules.enabled:
        return text
    out = text
    for pat in rules.device_patterns:
        if not pat.get("enabled", True):
            continue
        pattern = pat.get("pattern", "")
        suffix = pat.get("cabinet_suffix", "柜")
        m = re.match(pattern, out.strip())
        if m:
            cabinet, dev = m.group(1), m.group(2)
            out = f"{cabinet}{suffix}，{dev}"
            steps.append(f"设备模式: {text} → {out}")
            break
    return out


def _apply_char_rules(text: str, rules: SymbolRules, steps: list[str]) -> str:
    """字符替换：- → 杠"""
    if not rules.enabled:
        return text
    out = text
    for rule in rules.char_rules:
        if not rule.get("enabled", True):
            continue
        sym = rule.get("symbol", "")
        reading = rule.get("reading", "")
        if sym and reading and sym in out:
            out = out.replace(sym, reading)
            steps.append(f"字符: {sym} → {reading}")
    # 兼容旧逻辑：字母数字之间的连字符 → 杠
    out = re.sub(r"([A-Za-z0-9]+)-([A-Za-z0-9]+)", r"\1杠\2", out)
    return out


def _apply_word_rules(text: str, rules: SymbolRules, steps: list[str]) -> str:
    """词替换：up → 联通"""
    if not rules.enabled:
        return text
    out = text
    for rule in rules.word_rules:
        if not rule.get("enabled", True):
            continue
        src = rule.get("from", "")
        dst = rule.get("to", "")
        if not src:
            continue
        # 端口 up / 端口down 等组合
        new_out = re.sub(rf"端口\s*{re.escape(src)}\b", f"端口，{dst}", out, flags=re.IGNORECASE)
        if new_out != out:
            steps.append(f"词: 端口{src} → 端口，{dst}")
            out = new_out
        new_out = re.sub(rf"\b{re.escape(src)}\b", dst, out, flags=re.IGNORECASE)
        if new_out != out:
            steps.append(f"词: {src} → {dst}")
            out = new_out
    return out


def _polish_whitespace(text: str) -> str:
    """去多余空格、统一逗号。"""
    out = text.strip()
    out = out.replace("柜的", "柜，")
    out = re.sub(r"[ \t]+", "", out)
    out = re.sub(r"，{2,}", "，", out)
    return out


def normalize_for_cache(text: str, rules: SymbolRules | None = None) -> str:
    """
    规范化文本，作为缓存 Key 与 MOSS 合成输入。
    已是标准端口表格式的字符串将快速返回。
    """
    if not text:
        return text

    out = text.strip()
    # 标准端口表句式已规范化，直接返回
    if re.search(r"柜，.+，端口，(联通|中断)$", out):
        return out

    r = rules if rules is not None else load_symbol_rules()
    out = _apply_device_patterns(out, r, [])
    out = _apply_char_rules(out, r, [])
    out = _apply_word_rules(out, r, [])
    return _polish_whitespace(out)


def preview_normalize(text: str, rules: SymbolRules | None = None) -> tuple[str, list[str]]:
    """
    规范化预览（供 UI 符号读法弹窗）。
    返回 (规范化结果, 步骤说明列表)。
    """
    if not text:
        return text, []

    r = rules if rules is not None else load_symbol_rules()
    steps: list[str] = []
    out = text.strip()

    if re.search(r"柜，.+，端口，(联通|中断)$", out):
        steps.append("已是标准端口表格式，无需变更")
        return out, steps

    before = out
    out = _apply_device_patterns(out, r, steps)
    if out != before:
        before = out
    out = _apply_char_rules(out, r, steps)
    out = _apply_word_rules(out, r, steps)
    out = _polish_whitespace(out)
    if not steps:
        steps.append("无规则变更")
    return out, steps
