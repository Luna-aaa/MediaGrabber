"""
MediaGrabber 入口。

职责：托盘图标、全局热键、模式调度、全局异常捕获。

===========================================================================
 启动顺序是有讲究的，下面这几步的先后关系不能改，改了会出隐蔽的坐标 bug
===========================================================================
 第 1 步  抢占进程 DPI 感知模式（必须在任何 Qt / mss 相关代码之前）
          Windows 规定这个模式只能设一次、先到先得。mss 初始化时会抢着设成
          老的 system-aware 模式，一旦被它抢到，Qt 的几何计算全盘错位，
          而且不报错、不崩溃，只表现为「框选位置偏了一点」。
 第 2 步  配置日志（在 import config 之前，这样首次生成 config.json 的过程
          也能被记录下来）
 第 3 步  安装全局异常兜底
 第 4 步  创建 QApplication
 第 5 步  其余一切
===========================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让 python main.py 在任何工作目录下都能正确 import 本项目的包
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# --- 第 1 步：抢占 DPI 感知模式 ---------------------------------------------
# screen.geometry 只依赖 ctypes，不碰 Qt 也不碰 mss，可以安全地最早导入。
from screen import geometry  # noqa: E402

_DPI_SET_RESULT = geometry.set_per_monitor_v2_awareness()

# --- 第 2 步：日志 -----------------------------------------------------------
import logging  # noqa: E402

from core import logger as applog  # noqa: E402

_APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else _HERE
_LOG_PATH = applog.setup_logging(_APP_DIR, "INFO", 30)

log = logging.getLogger("main")

from config import CONFIG, app_dir  # noqa: E402

# 配置读出来之后，按用户设定的级别重新配一次日志
_LOG_PATH = applog.setup_logging(
    _APP_DIR,
    str(CONFIG.get("log.level", "INFO")),
    int(CONFIG.get("log.keep_days", 30) or 30),
)

# --- 第 3 步：全局异常兜底 ---------------------------------------------------
applog.install_exception_hooks()

# --- 第 4 步之后：正常导入 ---------------------------------------------------
import os  # noqa: E402
import socket  # noqa: E402

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon  # noqa: E402

from browser.controller import BrowserController  # noqa: E402
from core import downloader, naming  # noqa: E402
from core.history import History  # noqa: E402
from screen import capture  # noqa: E402
from screen.overlay import OverlayController  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.toast import LEVEL_ERROR, LEVEL_INFO, LEVEL_WARN, ToastManager  # noqa: E402

APP_NAME = "MediaGrabber"
# 单实例检测用的端口。随便挑的高位端口，只在本机 127.0.0.1 上监听。
SINGLE_INSTANCE_PORT = 52341


# ===========================================================================
# 全局热键
# ===========================================================================


class HotkeySignals(QObject):
    """
    热键回调 -> Qt 主线程的桥。

    pynput 的回调跑在它自己的线程里，那里**绝对不能碰任何 QWidget**（难点 8）。
    所以回调里只做一件事：emit 这个信号。Qt 会自动用 QueuedConnection 把它
    投递回主线程，槽函数在主线程执行，操作界面就安全了。
    """

    triggered = Signal()


class HotkeyManager:
    """
    全局热键的注册与开关。

    一个必须知道的坑：热键被别的软件占用时，pynput **不会报错**，
    只是静默失效。所以注册成功一定要写日志——你说「按了没反应」时，
    我第一件事就是看日志里有没有这行。
    """

    def __init__(self, combo: str):
        self.combo = combo
        self.signals = HotkeySignals()
        self._listener = None
        self._running = False

    def start(self) -> bool:
        """
        开始监听。已经在监听时直接返回 True。

        返回：是否注册成功。失败时已经写了日志，调用方负责提示用户。

        什么情况会失败：
            - pynput 没装
            - 热键字符串写错了（例如漏了尖括号）-> ValueError
            - 系统层面拒绝安装键盘钩子（极少见）
        """
        if self._running:
            return True
        try:
            from pynput import keyboard
        except ImportError as e:
            log.error("缺少 pynput，全局热键不可用：%s。请执行 pip install -r requirements.txt", e)
            return False

        try:
            self._listener = keyboard.GlobalHotKeys({self.combo: self._on_activate})
            self._listener.daemon = True
            self._listener.start()
            self._running = True
            log.info("全局热键注册成功：%s", self.combo)
            return True
        except ValueError as e:
            log.error(
                "热键字符串 %r 格式不对：%s。修饰键要写成 <ctrl> <alt> <shift>，"
                "例如 <ctrl>+<alt>+s。请修改 config.json 里的 hotkey.capture。",
                self.combo, e,
            )
        except Exception as e:
            log.exception("全局热键注册失败：%s", e)
        self._listener = None
        self._running = False
        return False

    def stop(self) -> None:
        """停止监听。遮罩显示期间会调它，避免热键叠加触发。"""
        if not self._running:
            return
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:
            log.exception("停止热键监听时出错（不影响使用）")
        finally:
            # pynput 的 listener 停掉之后不能重启，必须丢掉重建
            self._listener = None
            self._running = False
            log.debug("全局热键已暂停")

    def _on_activate(self) -> None:
        """pynput 线程里执行。只允许有这一行。"""
        self.signals.triggered.emit()


# ===========================================================================
# 托盘图标
# ===========================================================================


def make_tray_icon() -> QIcon:
    """
    用代码画一个托盘图标（一台小相机），省得依赖外部图标文件。

    画多个尺寸是为了在不同 DPI 的任务栏上都清晰。
    """
    icon = QIcon()
    for size in (16, 24, 32, 48, 64):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        s = size / 64.0
        p.setPen(Qt.PenStyle.NoPen)
        # 机身
        p.setBrush(QColor("#2563EB"))
        p.drawRoundedRect(2 * s, 14 * s, 60 * s, 44 * s, 8 * s, 8 * s)
        # 顶部取景器凸起
        p.drawRoundedRect(20 * s, 6 * s, 24 * s, 10 * s, 3 * s, 3 * s)
        # 镜头
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(int(18 * s), int(22 * s), int(28 * s), int(28 * s))
        p.setBrush(QColor("#1E3A8A"))
        p.drawEllipse(int(24 * s), int(28 * s), int(16 * s), int(16 * s))
        p.end()
        icon.addPixmap(pm)
    return icon


# ===========================================================================
# 主程序
# ===========================================================================


class MediaGrabberApp(QObject):
    """
    程序主体。串起托盘、热键、遮罩、落盘这条链路。

    所有槽函数都在主线程执行。
    """

    # 崩溃通知（可能从任意线程发出，靠 Qt 信号切回主线程）
    crashed = Signal(str)

    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.cfg = CONFIG

        self.toasts = ToastManager(int(self.cfg.get("ui.toast_duration_ms", 2000) or 2000))
        self.history = History(app_dir() / "history.jsonl")

        self.overlay = OverlayController(self.cfg, parent=self)
        self.overlay.captured.connect(self.on_captured)
        self.overlay.finished.connect(self.on_overlay_finished)

        self.hotkey = HotkeyManager(str(self.cfg.get("hotkey.capture", "<ctrl>+<alt>+s")))
        self.hotkey.signals.triggered.connect(self.on_hotkey)

        # 浏览器模式（阶段 2）。工作线程立刻起来，但不会自动连浏览器——
        # 只有你点了「启动浏览器」才会去开 Chrome。
        self.browser = BrowserController(self.cfg, self.history, parent=self)
        self.browser.notice.connect(self._on_browser_notice)
        self.browser.start()
        self.main_window: MainWindow | None = None

        self.crashed.connect(self._on_crashed)
        applog.set_crash_notifier(self.crashed.emit)

        self.tray = self._build_tray()

    # --- 托盘 ---------------------------------------------------------------

    def _build_tray(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(make_tray_icon(), self)
        tray.setToolTip(f"{APP_NAME} —— 按 {self.hotkey.combo} 开始框选")

        menu = QMenu()

        act_capture = QAction("立即框选", menu)
        act_capture.triggered.connect(self.on_hotkey)
        menu.addAction(act_capture)

        menu.addSeparator()

        act_main = QAction("浏览器模式（主窗口）", menu)
        act_main.triggered.connect(self.show_main_window)
        menu.addAction(act_main)

        act_settings = QAction("设置", menu)
        act_settings.triggered.connect(lambda: self._not_yet("设置面板", "阶段 3"))
        menu.addAction(act_settings)

        act_open = QAction("打开保存目录", menu)
        act_open.triggered.connect(self.open_save_dir)
        menu.addAction(act_open)

        act_log = QAction("打开日志目录", menu)
        act_log.triggered.connect(self.open_log_dir)
        menu.addAction(act_log)

        menu.addSeparator()

        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self.quit)
        menu.addAction(act_quit)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        return tray

    def _on_tray_activated(self, reason) -> None:
        """双击托盘图标 = 打开主窗口。"""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_main_window()

    def _not_yet(self, what: str, stage: str) -> None:
        self.toasts.show(f"{what}还没做", f"计划在{stage}提供", LEVEL_WARN)

    def show_main_window(self) -> None:
        """
        打开（或前置）浏览器模式主窗口。

        窗口是懒创建的：不点开就不占内存，也不会在启动时拖慢速度。
        关掉窗口不会退出程序，程序继续在托盘里待命。
        """
        if self.main_window is None:
            self.main_window = MainWindow(self.cfg, self.browser)
            # 首次使用提示：profile 目录还不存在，说明从没登录过
            if not self.cfg.profile_path().exists():
                self.main_window.show_first_run_hint()
        self.main_window.show()
        self.main_window.raise_()
        self.main_window.activateWindow()

    def _on_browser_notice(self, title: str, detail: str, level: str) -> None:
        """浏览器控制器要弹提示条（可能来自浏览器线程，靠信号已经切回主线程了）。"""
        self.toasts.show(title, detail, level)

    def open_save_dir(self) -> None:
        """在资源管理器里打开保存根目录。目录不存在就先建出来。"""
        root = self.cfg.save_root()
        try:
            naming.ensure_dir(root)
            os.startfile(str(root))
        except OSError as e:
            log.error("打开保存目录失败：%s", e)
            self.toasts.show("打不开保存目录", str(e), LEVEL_ERROR)

    def open_log_dir(self) -> None:
        """在资源管理器里打开日志目录。出问题时你直接从这里把文件发给我。"""
        try:
            os.startfile(str(_LOG_PATH.parent))
        except OSError as e:
            log.error("打开日志目录失败：%s", e)
            self.toasts.show("打不开日志目录", str(e), LEVEL_ERROR)

    # --- 热键 -> 遮罩 -------------------------------------------------------

    def on_hotkey(self) -> None:
        """
        热键被按下（已经切回主线程了）。

        流程：先停掉热键监听（防止遮罩期间重复触发），再弹遮罩。
        遮罩关闭后由 on_overlay_finished 把监听重新打开。
        """
        if self.overlay.active:
            log.debug("遮罩已经开着，忽略这次热键")
            return

        self.hotkey.stop()
        try:
            self.overlay.start()
        except Exception as e:
            log.exception("启动框选遮罩失败")
            self.toasts.show("无法开始框选", str(e), LEVEL_ERROR)
            # 遮罩没起来，finished 信号不会发，这里必须自己把热键恢复回去
            self.hotkey.start()

    def on_overlay_finished(self) -> None:
        """遮罩关掉了（不管是存了图还是取消），把全局热键重新打开。"""
        if not self.hotkey.start():
            self.toasts.show(
                "热键恢复失败",
                f"{self.hotkey.combo} 暂时不可用，可以从托盘菜单点「立即框选」",
                LEVEL_ERROR,
            )

    # --- 落盘 ---------------------------------------------------------------

    def on_captured(self, image, source: str) -> None:
        """
        拿到裁好的图，走公共落盘链路存到磁盘。

        参数：
            image  —— QImage，已经是物理像素、和屏幕内容一一对应
            source —— 来源屏幕名，写进历史记录

        全过程包在 try 里：保存失败必须弹红色提示条 + 写日志，绝不静默。
        """
        try:
            png = capture.qimage_to_png_bytes(image)
        except Exception as e:
            log.exception("PNG 编码失败")
            self.toasts.show("保存失败", f"图像编码失败：{e}", LEVEL_ERROR)
            return

        try:
            directory = naming.build_save_dir(
                self.cfg.save_root(),
                "screen",
                screen_subdir=str(self.cfg.get("save.screen_subdir", "Screen")),
                split_by_date=bool(self.cfg.get("save.split_by_date", True)),
            )
        except Exception as e:
            log.exception("计算保存目录失败")
            self.toasts.show("保存失败", str(e), LEVEL_ERROR)
            return

        result = downloader.save_bytes(
            png,
            directory,
            naming.screen_filename(),
            history=self.history,
            dedupe=bool(self.cfg.get("save.dedupe_by_md5", True)),
            max_filename_length=int(self.cfg.get("save.max_filename_length", 100) or 100),
            kind="screen",
            source=source,
            width=image.width(),
            height=image.height(),
        )

        if not result.ok:
            self.toasts.show("保存失败", result.error or "未知原因", LEVEL_ERROR)
        elif result.skipped:
            self.toasts.show(
                "已存在，已跳过",
                f"内容和之前存过的一样：{result.duplicate_of}",
                LEVEL_WARN,
            )
        else:
            assert result.path is not None
            self.toasts.show(
                f"{result.path.name}  ({image.width()}x{image.height()})",
                str(result.path.parent),
                LEVEL_INFO,
            )

    # --- 杂项 ---------------------------------------------------------------

    def _on_crashed(self, summary: str) -> None:
        """全局异常兜底触发时弹个红条，并告诉用户去哪找日志。"""
        self.toasts.show("程序内部出错了", f"{summary}\n详情见 logs 目录", LEVEL_ERROR)

    def greet(self) -> None:
        """启动完成后的第一条提示。"""
        self.toasts.show(
            f"{APP_NAME} 已启动",
            f"按 {self.hotkey.combo} 开始框选；图片存到 {self.cfg.save_root()}",
            LEVEL_INFO,
        )

    def quit(self) -> None:
        log.info("用户从托盘菜单退出")
        self.hotkey.stop()
        # 只断开 CDP 连接，**不关受控 Chrome**——你可能还在用那个浏览器窗口
        try:
            self.browser.shutdown()
        except Exception:
            log.debug("关闭浏览器控制器时出错，忽略", exc_info=True)
        if self.main_window is not None:
            self.main_window.close()
        self.toasts.clear()
        self.tray.hide()
        self.app.quit()


def acquire_single_instance_lock() -> socket.socket | None:
    """
    保证同时只有一个实例在跑。

    做法：在 127.0.0.1 上占一个端口。第二个实例占不到就知道自己是多余的。
    只绑本地回环地址，不对外开放，也不收发任何数据。

    为什么必须做：两个实例会同时注册同一个全局热键，表现是「按热键有时有反应
    有时没有」或者「一次截出两张图」——这种问题极难排查。

    返回：占住端口的 socket（调用方必须一直持有它，否则会被垃圾回收释放掉）；
    已经有实例在跑时返回 None。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # 不设 SO_REUSEADDR：这里就是要让第二个实例绑定失败
        sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


def main() -> int:
    log.info("=" * 70)
    log.info("%s 启动", APP_NAME)
    log.info("程序目录：%s", _APP_DIR)
    log.info("日志文件：%s", _LOG_PATH)
    log.info("Python：%s", sys.version.replace("\n", " "))
    # 下面这两行是排查坐标问题的第一现场，务必保留
    log.info("DPI 设置结果：%s", _DPI_SET_RESULT)
    log.info("DPI 实际生效：%s", geometry.describe_awareness())
    for m in geometry.enum_display_monitors():
        log.info("显示器（物理像素）：%s", m)
    log.info("虚拟桌面（物理像素）：%s", geometry.virtual_desktop_rect())
    log.info("=" * 70)

    if not geometry.awareness_is_per_monitor():
        log.error(
            "警告：进程未处于「每显示器 DPI 感知」，框选坐标可能不准。"
            "请检查是否有别的库在 main.py 之前抢先设置了 DPI 模式。"
        )

    lock = acquire_single_instance_lock()
    if lock is None:
        log.error("已经有一个 %s 在运行了，本次启动取消。", APP_NAME)
        print(f"{APP_NAME} 已经在运行（看一下右下角托盘区）。", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    # 没有主窗口，关掉最后一个窗口时绝对不能退出程序
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.error("当前系统没有可用的托盘区，程序无法运行。")
        print("系统托盘不可用，程序无法运行。", file=sys.stderr)
        return 2

    grabber = MediaGrabberApp(app)

    # 清掉上次崩溃可能留下的临时文件
    try:
        downloader.cleanup_stale_temp_files(
            naming.build_save_dir(
                grabber.cfg.save_root(), "screen",
                screen_subdir=str(grabber.cfg.get("save.screen_subdir", "Screen")),
                split_by_date=bool(grabber.cfg.get("save.split_by_date", True)),
            )
        )
    except Exception:
        log.debug("清理临时文件时出错，忽略", exc_info=True)

    if not grabber.hotkey.start():
        grabber.toasts.show(
            "全局热键注册失败",
            "可以从托盘菜单点「立即框选」，详情见 logs 目录",
            LEVEL_ERROR,
        )
    grabber.greet()

    log.info("进入 Qt 事件循环")
    code = app.exec()
    log.info("%s 退出，返回码 %s", APP_NAME, code)
    lock.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
