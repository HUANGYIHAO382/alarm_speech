"""
应用落盘日志模块
================
将运行日志、错误、未捕获异常写入项目 logs/ 目录，便于排查问题。

日志文件（均在项目根目录 logs/ 下）:
    - app.log      全量运行日志（INFO 及以上）
    - error.log    仅 ERROR 及以上
    - crash.log    未捕获异常（主线程 / 子线程）

用法:
    from app_logger import init_app_logging, get_logger
    init_app_logging()
    logger = get_logger("flet_demo")
    logger.info("程序启动")
"""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# 是否已完成全局初始化（避免重复添加 Handler）
_INITIALIZED = False
_LOG_DIR: Optional[Path] = None

# 单个日志文件最大 5MB，保留 5 个历史文件
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

# 统一日志格式：时间 | 级别 | 模块 | 消息
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def project_root() -> Path:
    """项目根目录（与本文件同级）。"""
    return Path(__file__).resolve().parent


def log_dir() -> Path:
    """日志目录路径。"""
    global _LOG_DIR
    if _LOG_DIR is None:
        _LOG_DIR = project_root() / "logs"
    return _LOG_DIR


def _make_file_handler(filename: str, level: int) -> RotatingFileHandler:
    """创建按大小滚动的文件 Handler。"""
    path = log_dir() / filename
    handler = RotatingFileHandler(
        path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    return handler


def _install_excepthooks(logger: logging.Logger) -> None:
    """捕获主线程与子线程未处理异常，写入 crash.log。"""

    def _write_crash(prefix: str, exc_type, exc_value, exc_tb) -> None:
        if exc_type is None:
            return
        lines = traceback.format_exception(exc_type, exc_value, exc_tb)
        body = "".join(lines)
        crash_logger = logging.getLogger("crash")
        crash_logger.error("%s\n%s", prefix, body)
        logger.error("%s: %s", prefix, exc_value)

    def _main_hook(exc_type, exc_value, exc_tb):
        _write_crash("未捕获异常(主线程)", exc_type, exc_value, exc_tb)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _main_hook

    # Python 3.8+ 子线程异常钩子
    if hasattr(threading, "excepthook"):
        def _thread_hook(args):
            _write_crash(
                f"未捕获异常(线程 {args.thread.name})",
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
            )

        threading.excepthook = _thread_hook  # type: ignore[attr-defined]


def init_app_logging(level: int = logging.INFO) -> logging.Logger:
    """
    初始化落盘日志（幂等，多次调用安全）。
    返回根 logger「alarm_speech」。
    """
    global _INITIALIZED
    log_dir().mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("alarm_speech")
    root.setLevel(level)

    if not _INITIALIZED:
        root.handlers.clear()
        root.addHandler(_make_file_handler("app.log", logging.INFO))
        root.addHandler(_make_file_handler("error.log", logging.ERROR))

        # crash 专用 logger，只写 error.log（通过父级传播）
        crash = logging.getLogger("crash")
        crash.setLevel(logging.ERROR)
        crash.propagate = True

        _install_excepthooks(root)
        _INITIALIZED = True
        root.info("=" * 60)
        root.info("日志系统初始化 | 目录: %s", log_dir())
        root.info("=" * 60)

    return root


def get_logger(name: str = "alarm_speech") -> logging.Logger:
    """获取子模块 logger，名称会出现在日志里。"""
    if not name.startswith("alarm_speech"):
        name = f"alarm_speech.{name}"
    return logging.getLogger(name)


def log_exception(logger: logging.Logger, message: str, exc: BaseException) -> str:
    """
    记录带堆栈的异常，返回适合显示在界面上的简短错误文本。
    """
    logger.exception("%s: %s", message, exc)
    return f"{message}: {exc}"


def log_ui_action(logger: logging.Logger, action: str, detail: str = "") -> None:
    """记录用户界面操作（便于复现问题）。"""
    if detail:
        logger.info("[UI] %s | %s", action, detail)
    else:
        logger.info("[UI] %s", action)


def session_log_path() -> str:
    """返回当前会话主日志文件路径（供界面展示）。"""
    return str(log_dir() / "app.log")


def tail_log_lines(max_lines: int = 80) -> str:
    """读取 app.log 末尾若干行，用于界面「查看日志」。"""
    path = log_dir() / "app.log"
    if not path.is_file():
        return f"（日志文件尚未生成: {path}）"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-max_lines:])
    except OSError as err:
        return f"读取日志失败: {err}"
