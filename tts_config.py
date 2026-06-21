"""
TTS 配置加载 (合并 config.json + config.local.json + 环境变量)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict


def project_root() -> Path:
    return Path(__file__).resolve().parent


def read_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_merged_config() -> Dict[str, Any]:
    root = project_root()
    base = read_config(root / "config.json")
    local = read_config(root / "config.local.json")
    return _deep_merge(base, local)


def get_xfyun_settings(config: dict | None = None) -> dict:
    cfg = config if config is not None else load_merged_config()
    tts = cfg.get("tts") or {}
    xf = tts.get("xfyun") or {}
    return {
        "app_id": (
            os.environ.get("XFYUN_APP_ID", "").strip()
            or str(xf.get("app_id", "")).strip()
        ),
        "api_key": (
            os.environ.get("XFYUN_API_KEY", "").strip()
            or str(xf.get("api_key", "")).strip()
        ),
        "api_secret": (
            os.environ.get("XFYUN_API_SECRET", "").strip()
            or str(xf.get("api_secret", "")).strip()
        ),
        "host_url": xf.get("host_url", "wss://tts-api.xfyun.cn/v2/tts"),
        "vcn": xf.get("vcn", "xiaoyan"),
        "auf": xf.get("auf", "audio/L16;rate=16000"),
        "speed": int(xf.get("speed", 50)),
        "volume": int(xf.get("volume", 50)),
        "pitch": int(xf.get("pitch", 50)),
        "tte": xf.get("tte", "UTF8"),
        "sample_rate": int(xf.get("sample_rate", 16000)),
    }


def create_xfyun_client(config: dict | None = None):
    from xfyun_tts import XfyunTtsClient

    s = get_xfyun_settings(config)
    return XfyunTtsClient(
        app_id=s["app_id"],
        api_key=s["api_key"],
        api_secret=s["api_secret"],
        host_url=s["host_url"],
        vcn=s["vcn"],
        auf=s["auf"],
        speed=s["speed"],
        volume=s["volume"],
        pitch=s["pitch"],
        tte=s["tte"],
        sample_rate=s["sample_rate"],
    )
