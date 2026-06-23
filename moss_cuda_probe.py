"""
供 tts_config 子进程调用的 CUDA 探测脚本。
退出码 0 = CUDA 真正可用；非 0 时在 stdout 打印原因。
"""

from __future__ import annotations

import sys

from moss_cuda_env import probe_cuda_ready


def main() -> int:
    ok, message = probe_cuda_ready()
    if ok:
        print("OK")
        return 0
    print(message)
    return 1


if __name__ == "__main__":
    sys.exit(main())
