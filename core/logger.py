"""
日志系统 + 全局异常兜底。

按 CLAUDE.md 的要求「不要静默失败」，这个模块负责保证：
出了任何问题，logs/ 目录下当天的文件里一定有记录，你可以直接把它发给我。

日志文件：
    logs/mediagrabber.log                当天的日志，永远是这个名字
    logs/mediagrabber-2026-08-29.log     昨天及更早的（每天零点自动滚动）

三层异常兜底（缺一不可，它们捕获的是不同来源的异常）：
    sys.excepthook        —— 主线程里没人接住的异常
    threading.excepthook  —— 子线程里没人接住的异常（Python 3.8+）
    qInstallMessageHandler—— Qt 自己吐出来的警告/错误（例如控件跨线程访问）
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable

LOG_DIR_NAME = "logs"
LOG_BASENAME = "mediagrabber.log"

_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 出现未捕获异常时的回调（由 main.py 注册，用来弹提示条告诉用户）
_crash_notifier: Callable[[str], None] | None = None


def _rotated_name(default_name: str) -> str:
    """
    把滚动后的文件名从 mediagrabber.log.2026-08-29 改成 mediagrabber-2026-08-29.log。

    纯粹是为了让你在文件管理器里一眼看懂哪个是哪天的，功能上没区别。
    """
    p = Path(default_name)
    # default_name 形如 ".../mediagrabber.log.2026-08-29"
    stem, _, date_part = p.name.rpartition(".")
    if not date_part or not stem.endswith(".log"):
        return default_name
    return str(p.with_name(f"{stem[:-4]}-{date_part}.log"))


def setup_logging(app_dir: Path, level: str = "INFO", keep_days: int = 30) -> Path:
    """
    初始化日志。必须在程序启动最早期调用一次。

    参数：
        app_dir   —— 程序目录，logs/ 会建在它下面
        level     —— DEBUG / INFO / WARNING / ERROR，来自 config.json
        keep_days —— 保留多少天的历史日志

    返回：当天日志文件的完整路径。

    什么情况会失败：
        logs/ 建不出来或没有写权限时，只往控制台输出并打印一条警告，
        程序继续跑（没日志总比起不来强）。
    """
    log_dir = app_dir / LOG_DIR_NAME
    root = logging.getLogger()

    # 重复调用时先清干净，避免日志重复输出
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    try:
        numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    except Exception:
        numeric_level = logging.INFO
    root.setLevel(numeric_level)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    # 控制台
    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    log_path = log_dir / LOG_BASENAME
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=str(log_path),
            when="midnight",
            interval=1,
            backupCount=max(1, int(keep_days)),
            encoding="utf-8",
            delay=False,
        )
        file_handler.suffix = "%Y-%m-%d"
        file_handler.namer = _rotated_name
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as e:
        root.warning("无法写入日志文件 %s（%s），本次只输出到控制台", log_path, e)

    return log_path


def set_crash_notifier(fn: Callable[[str], None] | None) -> None:
    """
    注册一个「出了未捕获异常时通知用户」的回调，通常是弹提示条。

    回调会在**异常发生的那个线程**里被调用，所以实现里必须是线程安全的
    （main.py 里用的是 Qt 信号，天然安全）。
    """
    global _crash_notifier
    _crash_notifier = fn


def _notify(summary: str) -> None:
    """调用崩溃通知回调。回调自己再炸就只记日志，绝不让兜底逻辑本身把程序搞崩。"""
    if _crash_notifier is None:
        return
    try:
        _crash_notifier(summary)
    except Exception:
        logging.getLogger(__name__).exception("崩溃通知回调本身也抛异常了")


def install_exception_hooks() -> None:
    """
    安装三层全局异常兜底。必须在 setup_logging 之后调用。

    注意：KeyboardInterrupt（Ctrl+C）会原样交给默认处理，不当成崩溃记录。
    """
    log = logging.getLogger("uncaught")

    previous_sys_hook = sys.excepthook

    def handle_sys(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous_sys_hook(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.critical("主线程未捕获异常：\n%s", text)
        _notify(f"{exc_type.__name__}: {exc_value}")

    def handle_thread(args):
        if issubclass(args.exc_type, SystemExit):
            return
        text = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        name = getattr(args.thread, "name", "未知线程")
        log.critical("子线程 %s 未捕获异常：\n%s", name, text)
        _notify(f"[{name}] {args.exc_type.__name__}: {args.exc_value}")

    sys.excepthook = handle_sys
    threading.excepthook = handle_thread

    _install_qt_handler(log)


def _install_qt_handler(log: logging.Logger) -> None:
    """
    把 Qt 自己的输出接到 Python 日志里。

    Qt 的警告（比如「在非 GUI 线程创建 QPixmap」）默认只打到 stderr，
    打包成 exe 之后 stderr 是看不到的，必须转进日志文件。
    """
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception as e:  # PySide6 没装或装坏了
        log.warning("无法安装 Qt 消息处理器：%s", e)
        return

    level_map = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    qt_log = logging.getLogger("Qt")

    def handler(msg_type, context, message):
        level = level_map.get(msg_type, logging.INFO)
        where = ""
        try:
            if context.file:
                where = f" ({context.file}:{context.line})"
        except Exception:
            pass
        qt_log.log(level, "%s%s", message, where)

    qInstallMessageHandler(handler)
