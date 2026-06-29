#!/usr/bin/env python3
"""
一键预热 Tier-0 固定词（CLI）
============================
用法（需 MOSS 守护进程可用）:
    python scripts/warmup_fixed_phrases.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 把项目根目录加入 path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from tts_engine import get_default_engine
    from tts_warmup_job import WarmupJob

    print("正在初始化 MOSS 引擎…")
    tts = get_default_engine()
    err = tts.warmup_moss()
    if err:
        print(f"MOSS 预热失败: {err}")
        return 1

    job = WarmupJob.instance()
    job.set_synthesize_fn(tts.synthesize_moss_bytes)
    n = job.enqueue_fixed_tier0()
    print(f"已加入固定词队列 {n} 条，后台合成中…")

    # 等待队列清空（简单轮询）
    import time
    for _ in range(600):
        state = job.get_state()
        if not state.get("running") and job._queue.empty():
            break
        time.sleep(2)

    print("固定词预热完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
