"""
受控 Chrome 的启动与 CDP 连接管理，以及页面扫描 / 批量下载的调度。

===========================================================================
 线程模型（这是本文件最重要的约束，改动前务必看懂）
===========================================================================
 playwright 的对象有严格的**线程亲和性**：Browser / Context / Page 全部只能在
 创建它们的那个 asyncio 事件循环里使用，跨线程调用会直接抛异常。

 所以本模块起了一个专用线程，里面跑一个 asyncio 事件循环，
 **所有 playwright 调用都在那个循环里**。Qt 主线程通过
 asyncio.run_coroutine_threadsafe() 把任务丢进去，结果通过 Qt 信号发回来。

     主线程（Qt）                     浏览器线程（asyncio）
     ─────────────                    ────────────────────
     connect() ──run_coroutine──▶     _do_connect()
                                      playwright / Chrome / CDP
     scan()    ──run_coroutine──▶     _do_scan()
     ◀────── Qt 信号（自动排队回主线程）──────

 铁律：本文件里任何 async 函数都**不许碰 QWidget**，只许 emit 信号。
===========================================================================

关于 Chrome 136+ 的限制（CLAUDE.md 第 6 节）：
从 Chrome 136 起，--remote-debugging-port 针对默认配置目录时会被直接忽略，
必须配一个指向非标准目录的 --user-data-dir。所以没法复用你平时那个 Chrome 的
登录状态，只能在受控浏览器里单独登录一次。这是 Chrome 的安全策略，不绕。
chrome-profile 目录会持久保留，登录一次之后一直有效。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from PySide6.QtCore import QObject, Signal

from browser import scanner
from core import downloader, naming
from core.downloader import SaveResult
from core.history import History

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# 连接状态。对应主窗口顶部指示灯的三种颜色。
STATE_DISCONNECTED = "disconnected"   # 灰
STATE_CONNECTING = "connecting"       # 黄
STATE_CONNECTED = "connected"         # 绿

# page.request 单次请求超时（毫秒）。
# 故意设得短：这条路一旦因为 DNS 污染 / 代理不通而卡住，每个新域名都要白等一次，
# 越久越拖慢整批下载。而兜底通道已经证明很可靠，就算是慢网络导致的误判也无所谓——
# 大不了这个域名之后都走兜底通道，结果一样对。
REQUEST_TIMEOUT_MS = 10000
# httpx 兜底路径的超时（秒）
HTTPX_TIMEOUT = 30.0
# 等 Chrome 起来并开放调试端口的最长时间（秒）
CHROME_BOOT_TIMEOUT = 25

# 判定「这是网络层面连不上」而不是「服务器返回了错误」的特征串。
# 命中这些说明 page.request 那条路根本没通到对方服务器，该换 httpx 试。
_NETWORK_ERROR_MARKERS = (
    "ETIMEDOUT", "ECONNREFUSED", "ECONNRESET", "ENOTFOUND", "EAI_AGAIN",
    "EHOSTUNREACH", "ENETUNREACH", "socket hang up", "Timeout", "timed out",
    "net::ERR_", "getaddrinfo", "tunneling socket",
)


def _is_network_level_error(exc: Exception) -> bool:
    """这个异常是不是「压根没连上对方服务器」。"""
    text = f"{type(exc).__name__}: {exc}"
    return any(marker.lower() in text.lower() for marker in _NETWORK_ERROR_MARKERS)


class FatalDownloadError(Exception):
    """
    不该重试的下载错误。

    401/403/404、DRM、分片流这些，重试多少次都是一样的结果，
    重试只会白白拖慢进度、给对方站点添麻烦。
    """


# ===========================================================================
# Chrome 定位与启动（同步函数，不涉及 asyncio）
# ===========================================================================


def find_chrome_executable(configured: str = "") -> Path | None:
    """
    找出本机 chrome.exe 的位置。

    参数 configured 是 config.json 里手动指定的路径，填了就优先用它。

    查找顺序：
        1. 配置里手动指定的
        2. 注册表 App Paths\\chrome.exe（HKLM 两个视图 + HKCU）
        3. Program Files / Program Files (x86) / LOCALAPPDATA 三个常见位置

    返回：找到的路径，全都找不到返回 None（调用方负责提示用户）。
    """
    if configured:
        p = Path(configured)
        if p.is_file():
            return p
        log.warning("config.json 里指定的 chrome_path 不存在：%s，改用自动探测", p)

    candidates: list[Path] = []

    try:
        import winreg

        sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, sub, 0),
            (winreg.HKEY_LOCAL_MACHINE, sub, getattr(winreg, "KEY_WOW64_32KEY", 0)),
            (winreg.HKEY_CURRENT_USER, sub, 0),
        ]
        for root, key_path, flag in roots:
            try:
                access = winreg.KEY_READ | flag
                with winreg.OpenKey(root, key_path, 0, access) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                    if value:
                        candidates.append(Path(value))
            except OSError:
                continue
    except ImportError:
        pass  # 非 Windows，直接走常见路径

    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")

    for p in candidates:
        try:
            if p.is_file():
                log.info("找到 Chrome：%s", p)
                return p
        except OSError:
            continue

    log.error("没能找到 chrome.exe，尝试过的位置：%s", [str(c) for c in candidates])
    return None


def detect_proxy() -> tuple[str | None, str]:
    """
    找出该用哪个代理。返回 (代理地址, 来源说明)，没有代理时返回 (None, 说明)。

    为什么不能只看环境变量（这是个真实踩过的坑）：
    很多代理软件（Clash 等）只改 **Windows 系统代理设置**，不设环境变量；
    就算设了，那也往往只存在于某个终端会话里——你从资源管理器双击 exe 启动的
    进程根本继承不到。结果就是：浏览器好好的，程序却连不上，
    报 ConnectError: All connection attempts failed。

    所以查找顺序和 Chrome 保持一致：
        1. 环境变量（用户显式指定的，优先级最高）
        2. Windows 系统代理设置（注册表 Internet Settings，Chrome 读的就是它）
    """
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        value = (os.environ.get(var) or "").strip()
        if value:
            if "://" not in value:
                value = "http://" + value
            return value, f"环境变量 {var}"

    if not IS_WINDOWS:
        return None, "未配置代理"

    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None, "Windows 系统代理未启用"
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None, "读不到 Windows 代理设置"

    server = str(server or "").strip()
    if not server:
        return None, "Windows 系统代理为空"

    # ProxyServer 有两种写法：
    #   "127.0.0.1:7890"                        所有协议共用
    #   "http=1.2.3.4:80;https=5.6.7.8:443"     按协议分开
    if "=" in server:
        table = {}
        for part in server.split(";"):
            if "=" in part:
                scheme, _, addr = part.partition("=")
                table[scheme.strip().lower()] = addr.strip()
        server = table.get("https") or table.get("http") or ""
        if not server:
            return None, "Windows 系统代理里没有 http/https 条目"

    if "://" not in server:
        server = "http://" + server
    return server, "Windows 系统代理设置"


def ensure_localhost_bypasses_proxy() -> None:
    """
    确保本机地址不走代理。

    背景：很多代理软件（Clash、v2ray 等）会设置 HTTP_PROXY / HTTPS_PROXY
    环境变量。不少 HTTP 库看到这两个变量就无脑走代理，连访问 127.0.0.1 也不例外，
    结果代理返回 502，表现为「Chrome 明明起来了，程序却说连不上」。

    这里往 NO_PROXY 里补上本机地址（保留用户原有的内容），
    给所有尊重这个变量的库兜个底。我们自己的 httpx 调用另外还加了
    trust_env=False，双保险。
    """
    needed = ["127.0.0.1", "localhost", "::1"]
    for var in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(var, "")
        parts = [p.strip() for p in current.split(",") if p.strip()]
        missing = [h for h in needed if h not in parts]
        if missing:
            os.environ[var] = ",".join(parts + missing)
    log.debug("NO_PROXY 已设置为：%s", os.environ.get("NO_PROXY"))


def launch_chrome(exe: Path, port: int, profile_dir: Path) -> subprocess.Popen | None:
    """
    启动受控 Chrome。

    参数：
        exe         —— chrome.exe 路径
        port        —— 调试端口
        profile_dir —— --user-data-dir 指向的目录。**这个参数是强制的**，
                       Chrome 136+ 没有它就不会开调试端口。

    返回：Popen 对象；启动失败返回 None（已写日志）。

    注意：这里故意不清空 profile_dir。它保存着你在受控浏览器里的登录状态，
    每次启动都清的话，你就得每次重新登录。
    """
    args = [
        str(exe),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    log.info("启动受控 Chrome：%s", " ".join(args))
    try:
        return subprocess.Popen(args, close_fds=True)
    except OSError as e:
        log.error("启动 Chrome 失败：%s", e)
        return None


# ===========================================================================
# 控制器
# ===========================================================================


class BrowserController(QObject):
    """
    浏览器模式的总控。所有公开方法都从 Qt 主线程调用，内部转到浏览器线程执行。

    信号一览（全部在主线程接收）：
        state_changed(state, message)     连接状态变化，驱动指示灯
        notice(title, detail, level)      需要弹提示条的消息
        first_run_needed()                检测到是第一次用，要提示去登录
        scan_finished(list)               扫描完成，参数是媒体条目列表
        scan_failed(str)                  扫描失败
        download_progress(done, total)    下载进度
        download_item_result(idx, ok, msg) 单项下载结果，用于给网格标红
        download_finished(ok, failed, skipped)
        login_domains(list)               当前有 cookie 的域名列表
    """

    state_changed = Signal(str, str)
    notice = Signal(str, str, str)
    first_run_needed = Signal()
    scan_finished = Signal(list)
    scan_failed = Signal(str)
    download_progress = Signal(int, int)
    download_item_result = Signal(int, bool, str)
    download_finished = Signal(int, int, int)
    login_domains = Signal(list)
    thumbnail_ready = Signal(int, bytes)

    def __init__(self, cfg, history: History, parent: QObject | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self.history = history

        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop, name="browser-loop", daemon=True
        )

        # 下面这些只在浏览器线程里访问
        self._pw = None
        self._browser = None
        self._context = None
        self._chrome_proc: subprocess.Popen | None = None
        self._rate_lock: asyncio.Lock | None = None
        self._last_request_at = 0.0
        self._cancel_download = False
        # 共享的 httpx 客户端（兜底下载通道），懒创建
        self._httpx_client = None
        self._user_agent: str | None = None
        # 在 page.request 上栽过跟头的域名。之后同域名直接走 httpx，
        # 免得整批图片每张都先白等一次超时。
        self._prefer_httpx_hosts: set[str] = set()

        self._state = STATE_DISCONNECTED
        self._busy = False
        self._scan_time = 0.0
        # 每次新扫描 +1。旧的缩略图任务看到代次变了就自己退出，
        # 免得上一次扫描的缩略图错位贴到新结果上。
        self._thumb_generation = 0

    # --- 线程与任务提交 -----------------------------------------------------

    def start(self) -> None:
        """启动浏览器线程。程序启动时调一次即可，不会连接浏览器。"""
        if self._thread.is_alive():
            return
        self._thread.start()
        if not self._ready.wait(timeout=5):
            log.error("浏览器工作线程启动超时")

    def _run_loop(self) -> None:
        """浏览器线程的主体：跑一个永不退出的 asyncio 事件循环。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._rate_lock = asyncio.Lock()
        self._ready.set()
        log.info("浏览器工作线程已就绪")
        try:
            self._loop.run_forever()
        finally:
            log.info("浏览器工作线程退出")

    def _submit(self, coro) -> None:
        """
        把一个协程丢进浏览器线程执行。

        什么情况会失败：线程还没起来 -> 只写日志并提示，不抛异常，
        因为调用方是界面按钮，不该因为这个崩掉。
        """
        if self._loop is None or not self._loop.is_running():
            log.error("浏览器工作线程未就绪，无法执行任务")
            self.notice.emit("浏览器模式不可用", "内部工作线程未启动，请重启程序", "error")
            coro.close()
            return
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._on_task_done)

    def _on_task_done(self, future) -> None:
        """统一兜住浏览器线程里逃出来的异常，绝不让它静默消失。"""
        try:
            future.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("浏览器任务出错")
            self.notice.emit("操作失败", f"{type(e).__name__}: {e}", "error")

    def _set_state(self, state: str, message: str = "") -> None:
        self._state = state
        log.info("浏览器连接状态：%s %s", state, message)
        self.state_changed.emit(state, message)

    @property
    def state(self) -> str:
        return self._state

    @property
    def connected(self) -> bool:
        return self._state == STATE_CONNECTED

    # --- 公开操作（主线程调用） ---------------------------------------------

    def connect_browser(self) -> None:
        """连接受控 Chrome，没起就先启动它。"""
        self._submit(self._do_connect())

    def scan_page(self) -> None:
        """扫描当前活动标签页。"""
        self._submit(self._do_scan())

    def download_items(self, items: list[dict[str, Any]]) -> None:
        """批量下载选中的条目。items 是 scan_finished 给出的那些字典。"""
        self._cancel_download = False
        self._submit(self._do_download(items))

    def cancel_download(self) -> None:
        """请求取消当前批量下载。已经在传的那几个会跑完，后面的不再开始。"""
        self._cancel_download = True
        log.info("用户请求取消下载")

    def load_thumbnails(self, items: list[dict[str, Any]]) -> None:
        """给扫描结果抓缩略图。逐条通过 thumbnail_ready 信号发回来。"""
        if not bool(self.cfg.get("browser.load_thumbnails", True)):
            log.info("配置里关掉了缩略图，跳过")
            return
        self._thumb_generation += 1
        self._submit(self._do_thumbnails(items, self._thumb_generation))

    def refresh_login_domains(self) -> None:
        """刷新「哪些站点已登录」的域名列表。"""
        self._submit(self._do_login_domains())

    def shutdown(self) -> None:
        """程序退出时调用。关掉 playwright 并停掉事件循环，但**不关 Chrome**。"""
        if self._loop is None or not self._loop.is_running():
            return
        fut = asyncio.run_coroutine_threadsafe(self._do_shutdown(), self._loop)
        try:
            fut.result(timeout=5)
        except Exception:
            log.debug("关闭 playwright 时出错，忽略", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)

    # --- 连接 ---------------------------------------------------------------

    async def _do_connect(self) -> None:
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    self._set_state(STATE_CONNECTED, "已经连上了")
                    return
            except Exception:
                pass
            await self._teardown_playwright()

        ensure_localhost_bypasses_proxy()
        # 网络环境可能变了（换 WiFi、开关代理），重连时把「该走 httpx 的域名」清空重来
        self._prefer_httpx_hosts.clear()
        port = int(self.cfg.get("browser.debug_port", 9222) or 9222)
        self._set_state(STATE_CONNECTING, f"正在检测调试端口 {port}")

        if not await self._probe_port(port, timeout=1.5):
            exe = find_chrome_executable(str(self.cfg.get("browser.chrome_path", "")))
            if exe is None:
                self._set_state(STATE_DISCONNECTED, "找不到 Chrome")
                self.notice.emit(
                    "找不到 Chrome",
                    "请先安装 Chrome，或在 config.json 的 browser.chrome_path 里手动填路径",
                    "error",
                )
                return

            profile = self.cfg.profile_path()
            is_first_run = not profile.exists()
            self._set_state(STATE_CONNECTING, "正在启动受控 Chrome")
            self._chrome_proc = launch_chrome(exe, port, profile)
            if self._chrome_proc is None:
                self._set_state(STATE_DISCONNECTED, "Chrome 启动失败")
                self.notice.emit("启动 Chrome 失败", "详情见 logs 目录", "error")
                return

            if is_first_run:
                log.info("检测到首次使用（profile 目录原先不存在）：%s", profile)
                self.first_run_needed.emit()

            if not await self._wait_port(port, CHROME_BOOT_TIMEOUT):
                self._set_state(STATE_DISCONNECTED, "Chrome 调试端口没开起来")
                self.notice.emit(
                    "连接失败",
                    f"等了 {CHROME_BOOT_TIMEOUT} 秒，Chrome 的调试端口 {port} 仍然没开。\n"
                    "如果你已经开着一个普通 Chrome，请先把它全部关掉再试——"
                    "Chrome 会把新窗口交给已有进程处理，导致调试端口开不起来。",
                    "error",
                )
                return

        # --- CDP 连接 ---
        self._set_state(STATE_CONNECTING, "正在建立 CDP 连接")
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self._set_state(STATE_DISCONNECTED, "playwright 未安装")
            self.notice.emit(
                "缺少 playwright",
                "请执行：pip install -r requirements.txt",
                "error",
            )
            return

        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )
        except Exception as e:
            log.exception("CDP 连接失败")
            await self._teardown_playwright()
            self._set_state(STATE_DISCONNECTED, "CDP 连接失败")
            self.notice.emit("连接浏览器失败", str(e), "error")
            return

        self._browser.on("disconnected", self._on_browser_disconnected)
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else await self._browser.new_context()

        self._set_state(STATE_CONNECTED, f"已连接（{len(self._context.pages)} 个标签页）")
        await self._do_login_domains()

    def _on_browser_disconnected(self, *_args) -> None:
        """
        Chrome 被关掉时由 playwright 回调。

        这是 CLAUDE.md 要求的「我手动关掉浏览器时要能感知，程序不许崩溃」。
        这里只改状态 + 发信号，不做任何清理动作——清理留给下次连接时做，
        因为回调发生在 playwright 内部，此时去 await 它的对象容易再炸一次。
        """
        log.warning("受控 Chrome 已断开连接")
        self._browser = None
        self._context = None
        self._set_state(STATE_DISCONNECTED, "浏览器已关闭")
        self.notice.emit("浏览器已断开", "受控 Chrome 被关掉了，点「启动浏览器」可以重连", "warn")

    async def _teardown_playwright(self) -> None:
        if self._httpx_client is not None:
            try:
                await self._httpx_client.aclose()
            except Exception:
                log.debug("关闭 httpx 客户端时出错，忽略", exc_info=True)
            self._httpx_client = None
        self._user_agent = None
        for name in ("_browser", "_pw"):
            obj = getattr(self, name, None)
            if obj is None:
                continue
            try:
                await (obj.close() if name == "_browser" else obj.stop())
            except Exception:
                log.debug("关闭 %s 时出错，忽略", name, exc_info=True)
            setattr(self, name, None)
        self._context = None

    async def _do_shutdown(self) -> None:
        await self._teardown_playwright()

    async def _probe_port(self, port: int, timeout: float = 1.5) -> bool:
        """
        探一下调试端口通不通。用的是 Chrome 自带的 /json/version 接口。

        **trust_env=False 是必须的，别删。**
        很多人机器上设了 HTTP_PROXY / HTTPS_PROXY（各种代理软件都会设）。
        httpx 默认会读这两个环境变量，结果连访问 127.0.0.1 都被送去走代理，
        代理不认识这个地址就返回 502 —— 表现为「Chrome 明明起来了，
        程序却说连不上」。关掉 trust_env 就直连，不受任何代理设置影响。
        """
        try:
            import httpx
        except ImportError:
            return False
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                r = await client.get(f"http://127.0.0.1:{port}/json/version")
                return r.status_code == 200
        except Exception:
            return False

    async def _wait_port(self, port: int, seconds: int) -> bool:
        """轮询等调试端口开起来。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if await self._probe_port(port, timeout=1.0):
                return True
            await asyncio.sleep(0.5)
        return False

    # --- 扫描 ---------------------------------------------------------------

    async def _active_page(self):
        """
        找出当前活动的标签页。

        CDP 没有直接的「哪个是活动标签」接口，所以用页面自己的状态判断：
        前台标签的 document.visibilityState 是 "visible"，后台标签是 "hidden"。
        再用 document.hasFocus() 在多个可见页面（多窗口）里挑真正聚焦的那个。

        什么情况会失败：一个网页都没开时抛 RuntimeError。
        """
        if self._context is None:
            raise RuntimeError("还没连接浏览器，请先点「启动浏览器」")

        pages = [
            p for p in self._context.pages
            if not str(p.url).startswith(("devtools://", "chrome://", "chrome-extension://"))
        ]
        if not pages:
            raise RuntimeError("受控浏览器里没有打开任何网页，请先在里面打开你要抓的页面")

        visible = []
        for p in pages:
            try:
                st = await p.evaluate("() => document.visibilityState")
                focused = await p.evaluate("() => document.hasFocus()")
            except Exception:
                continue
            if st == "visible":
                visible.append((p, bool(focused)))

        for p, focused in visible:
            if focused:
                return p
        if visible:
            return visible[0][0]
        return pages[-1]

    async def _do_scan(self) -> None:
        try:
            page = await self._active_page()
        except RuntimeError as e:
            self.scan_failed.emit(str(e))
            return

        url = page.url
        log.info("开始扫描页面：%s", url)
        raw: list[dict[str, Any]] = []
        frames = page.frames
        for frame in frames:
            try:
                items = await frame.evaluate(scanner.SCAN_JS)
            except Exception as e:
                # 跨域 iframe、已经销毁的 frame 都会走到这里，属于正常情况
                log.debug("frame %s 扫描跳过：%s", getattr(frame, "url", "?"), e)
                continue
            if isinstance(items, list):
                raw.extend(x for x in items if isinstance(x, dict))

        kept, stats = scanner.filter_items(
            raw,
            min_width=int(self.cfg.get("browser.min_width", 200) or 200),
            min_height=int(self.cfg.get("browser.min_height", 200) or 200),
        )
        self._scan_time = time.monotonic()
        log.info(
            "扫描完成：%d 个 frame，原始 %d 条，保留 %d 条，丢弃明细 %s",
            len(frames), len(raw), len(kept), stats,
        )
        self.scan_finished.emit(kept)

    # --- 下载 ---------------------------------------------------------------

    async def _rate_limit(self) -> None:
        """
        全局请求间隔控制。

        CLAUDE.md 第 12 节要求「不要把目标站点打崩」，所以除了并发上限，
        还要保证任意两次请求之间至少隔 request_interval_ms。
        """
        interval = max(0, int(self.cfg.get("browser.request_interval_ms", 200) or 0)) / 1000.0
        if interval <= 0 or self._rate_lock is None:
            return
        async with self._rate_lock:
            wait = self._last_request_at + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    async def _do_download(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        try:
            page = await self._active_page()
        except RuntimeError as e:
            self.notice.emit("下载失败", str(e), "error")
            return

        # 签名过期提醒（难点 9）
        stale_min = float(self.cfg.get("browser.scan_stale_minutes", 5) or 5)
        if self._scan_time and (time.monotonic() - self._scan_time) > stale_min * 60:
            self.notice.emit(
                "扫描结果可能已过期",
                f"距上次扫描已超过 {stale_min:.0f} 分钟。带 token 的直链有时效，"
                "如果大量失败，请重新扫描页面。",
                "warn",
            )

        concurrency = max(1, int(self.cfg.get("browser.concurrency", 3) or 3))
        total = len(items)
        counters = {"ok": 0, "failed": 0, "skipped": 0}
        done = 0
        sem = asyncio.Semaphore(concurrency)
        lock = asyncio.Lock()

        log.info("开始批量下载：%d 项，并发 %d", total, concurrency)

        async def worker(index: int, item: dict[str, Any]) -> None:
            nonlocal done
            if self._cancel_download:
                return
            async with sem:
                if self._cancel_download:
                    return
                await self._rate_limit()
                result = await self._download_one(page, item)
            async with lock:
                done += 1
                if not result.ok:
                    counters["failed"] += 1
                elif result.skipped:
                    counters["skipped"] += 1
                else:
                    counters["ok"] += 1
                self.download_item_result.emit(index, result.ok, result.message())
                self.download_progress.emit(done, total)

        await asyncio.gather(
            *(worker(i, it) for i, it in enumerate(items)), return_exceptions=True
        )
        log.info("批量下载结束：成功 %(ok)d 跳过 %(skipped)d 失败 %(failed)d", counters)
        self.download_finished.emit(counters["ok"], counters["failed"], counters["skipped"])

    async def _download_one(self, page, item: dict[str, Any]) -> SaveResult:
        """
        下载并保存一个条目。带重试。绝不抛异常，一律转成 SaveResult。
        """
        url = str(item.get("url") or "")
        note = str(item.get("note") or "")

        if note == scanner.NOTE_DRM:
            return SaveResult(ok=False, error="DRM 加密内容，本工具不支持（不会重试）")
        if note == scanner.NOTE_STREAM:
            return SaveResult(
                ok=False,
                error="这是分片流媒体（blob:），不是可下载的视频文件，本工具不支持",
            )

        directory = naming.build_save_dir(
            self.cfg.save_root(),
            "browser",
            domain=str(item.get("referrer") or url),
            split_by_domain=bool(self.cfg.get("save.split_by_domain", True)),
            split_by_date=bool(self.cfg.get("save.split_by_date", True)),
        )

        retries = max(0, int(self.cfg.get("browser.retry", 2) or 0))
        last_error: str = "未知错误"

        for attempt in range(retries + 1):
            if self._cancel_download:
                return SaveResult(ok=False, error="已取消")
            try:
                if str(item.get("kind")) == scanner.KIND_VIDEO and url.startswith("http"):
                    # 视频可能很大，走流式，不整个读进内存
                    return await self._stream_to_file(item, directory)

                data, content_type = await self._fetch_bytes(page, item)
                ext = scanner.guess_extension(url, content_type)
                filename = naming.filename_from_url(url, ext)
                # save_bytes 是阻塞的磁盘 IO，丢到线程里做，别卡住事件循环
                return await asyncio.to_thread(
                    downloader.save_bytes,
                    data, directory, filename,
                    history=self.history,
                    dedupe=bool(self.cfg.get("save.dedupe_by_md5", True)),
                    max_filename_length=int(self.cfg.get("save.max_filename_length", 100) or 100),
                    kind="browser",
                    source=url,
                    width=int(item.get("width") or 0),
                    height=int(item.get("height") or 0),
                )
            except FatalDownloadError as e:
                log.info("放弃下载 %s：%s", url[:120], e)
                return SaveResult(ok=False, error=str(e))
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                log.warning("下载失败（第 %d 次）%s：%s", attempt + 1, url[:120], last_error)
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))   # 退避，别死磕

        return SaveResult(ok=False, error=f"重试 {retries} 次后仍失败 —— {last_error}")

    async def _fetch_bytes(self, page, item: dict[str, Any]) -> tuple[bytes, str]:
        """按地址类型取回字节。返回 (数据, Content-Type)。"""
        url = str(item.get("url") or "")
        if url.startswith("data:"):
            return self._decode_data_url(url)
        if url.startswith("blob:"):
            return await self._fetch_blob(page, url)
        return await self._fetch_http(page, item)

    @staticmethod
    def _decode_data_url(url: str) -> tuple[bytes, str]:
        """解 data: 地址。不发任何网络请求。"""
        try:
            header, _, payload = url[5:].partition(",")
        except Exception as e:
            raise FatalDownloadError(f"data: 地址格式不对：{e}") from e
        content_type = header.split(";")[0]
        if ";base64" in header:
            try:
                return base64.b64decode(payload), content_type
            except Exception as e:
                raise FatalDownloadError(f"data: 地址的 base64 解码失败：{e}") from e
        from urllib.parse import unquote_to_bytes
        return unquote_to_bytes(payload), content_type

    async def _fetch_blob(self, page, url: str) -> tuple[bytes, str]:
        """
        取 blob: 地址（难点 4）。

        blob: 是页面内部的临时地址，外部完全取不到，只能让页面自己 fetch 出来，
        转成 dataURL 字符串回传，再在 Python 端解码。

        两个限制：
          - base64 传输会膨胀约 33%，太大的直接拒绝，否则会把内存和 CDP 通道撑爆
          - MediaSource 产生的 blob（视频流）fetch 必然失败，这时明确告诉用户不支持
        """
        limit_mb = float(self.cfg.get("browser.blob_max_mb", 50) or 50)
        js = """
        async ([url, limitBytes]) => {
          const r = await fetch(url);
          const b = await r.blob();
          if (b.size > limitBytes) throw new Error("TOO_BIG:" + b.size);
          return await new Promise((res, rej) => {
            const fr = new FileReader();
            fr.onload = () => res(fr.result);
            fr.onerror = () => rej(new Error("READ_FAILED"));
            fr.readAsDataURL(b);
          });
        }
        """
        try:
            data_url = await page.evaluate(js, [url, int(limit_mb * 1024 * 1024)])
        except Exception as e:
            msg = str(e)
            if "TOO_BIG" in msg:
                raise FatalDownloadError(f"文件超过 {limit_mb:.0f}MB，blob 方式取不了") from e
            raise FatalDownloadError(
                "这个 blob 地址取不到内容，通常说明它是 MediaSource 分片流"
                "（视频网站的常见做法），本工具不支持这类视频"
            ) from e
        return self._decode_data_url(str(data_url))

    @staticmethod
    def _describe_status(status: int, both_channels: bool) -> str:
        """把 HTTP 状态码翻译成人话。"""
        tail = "（两条下载通道都试过了）" if both_channels else ""
        if status in (401, 407):
            return f"HTTP {status}：可能是登录已过期，请在受控浏览器中重新登录该网站{tail}"
        if status == 403:
            if both_channels:
                return (
                    "HTTP 403：两条通道都被服务器拒绝。如果这是网站自己的界面素材"
                    "（图标、按钮、装饰图），那是网站本来就不允许直接下载，重新扫描也没用；"
                    "如果是内容图，可能是防盗链或带 token 的直链已过期，可以试试重新扫描"
                )
            return "HTTP 403：被服务器拒绝，可能是防盗链或直链已过期"
        if status == 404:
            return f"HTTP 404：资源不存在，链接可能已失效，试试重新扫描页面{tail}"
        if status == 429:
            return f"HTTP 429：请求太频繁被限流了，过一会儿再试{tail}"
        return f"HTTP {status}{tail}"

    def _raise_for_status(self, status: int, both_channels: bool) -> None:
        """4xx 判死不重试；5xx 是服务端临时问题，交给外层重试。"""
        if status < 400:
            return
        message = self._describe_status(status, both_channels)
        if status < 500:
            raise FatalDownloadError(message)
        raise RuntimeError(message)

    async def _fetch_http(self, page, item: dict[str, Any]) -> tuple[bytes, str]:
        """
        下载一个 http(s) 资源。两条路，优先第一条：

        1. page.request —— 共享 browser context 的完整会话（cookie、UA 全自动正确），
           这是登录站点能下载成功的关键，所以是首选。

        2. httpx 兜底 —— 手动带上 Referer、真实 UA、context 里的 cookie。

        为什么第二条路是必需的（CLAUDE.md 第 6.3 节要求的降级）：
        page.request 是 Playwright 的 Node 进程自己发的请求，**它不走 Chrome 的
        网络栈**——不用 Chrome 的代理、自己做 DNS 解析。在 DNS 被污染或者需要
        代理才能访问的网络环境里，页面上图片明明显示正常，page.request 却会
        connect ETIMEDOUT。httpx 默认读 HTTP_PROXY 环境变量，走系统代理，
        DNS 由代理远程解析，正好绕开这两个问题。

        另外：某个域名一旦在第一条路上栽了，就把它记下来，之后同域名的资源
        直接走 httpx。否则一整批图片每张都要先白等一次超时。
        """
        url = str(item.get("url") or "")
        referer = str(item.get("referrer") or "")
        host = (urlparse(url).hostname or "").lower()

        if host and host in self._prefer_httpx_hosts:
            return await self._fetch_via_httpx(url, referer)

        # 第一条通道的 HTTP 错误码。拿到值说明「连上了但被拒绝」，
        # 这时也要换第二条通道再试一次——两条通道的请求头和出口 IP 都不一样，
        # 常有第一条 403、第二条 200 的情况。都失败了才判死。
        first_status: int | None = None
        try:
            headers = {"Accept": "image/*,video/*,*/*;q=0.8"}
            if referer:
                headers["Referer"] = referer
            resp = await page.request.get(url, headers=headers, timeout=REQUEST_TIMEOUT_MS)
            if resp.status >= 400:
                first_status = resp.status
                log.info(
                    "page.request 返回 HTTP %s，换 httpx 通道再试一次：%s",
                    resp.status, url[:120],
                )
            else:
                return await resp.body(), resp.headers.get("content-type", "")
        except FatalDownloadError:
            raise
        except Exception as e:
            if not _is_network_level_error(e):
                raise
            if host and host not in self._prefer_httpx_hosts:
                self._prefer_httpx_hosts.add(host)
                log.warning(
                    "page.request 连不上 %s（%s）。这通常是 DNS 污染或需要代理导致的；"
                    "该域名后续一律改走 httpx。",
                    host, e,
                )

        return await self._fetch_via_httpx(url, referer, first_status=first_status)

    async def _http_client(self):
        """
        懒创建一个共享的 httpx 客户端（兜底下载通道）。

        整批下载共用一个，连接可以复用，比每次新建快很多。

        代理是**显式**传进去的，不靠 httpx 自己读环境变量：
        很多代理软件只改 Windows 系统代理、不设环境变量，而且从资源管理器
        双击启动的进程也继承不到终端里的环境变量。走 detect_proxy() 能覆盖
        这两种情况——它和 Chrome 读的是同一份配置，所以「浏览器能看到的图，
        这条通道也能下下来」。

        本机地址（127.0.0.1 / localhost）单独挂直连，否则抓本地服务会被
        绕去代理然后失败。
        """
        if self._httpx_client is not None:
            return self._httpx_client

        import httpx

        proxy_url, source = detect_proxy()
        if proxy_url:
            log.info("兜底下载通道将使用代理：%s（来源：%s）", proxy_url, source)
            direct = httpx.AsyncHTTPTransport(retries=1)
            through_proxy = httpx.AsyncHTTPTransport(proxy=httpx.Proxy(proxy_url), retries=1)
            mounts = {
                "all://": through_proxy,
                # 本机一律直连
                "all://localhost": direct,
                "all://127.0.0.1": direct,
                "all://[::1]": direct,
            }
        else:
            log.info("兜底下载通道不使用代理（%s）", source)
            mounts = None

        self._httpx_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=HTTPX_TIMEOUT,
            mounts=mounts,
            trust_env=False,      # 代理已经显式指定，不要再让它读环境变量
        )
        return self._httpx_client

    async def _session_headers(self, url: str, referer: str, accept: str) -> dict[str, str]:
        """
        给 httpx 拼一套「看起来就是浏览器发的」请求头。

        Referer 对付防盗链（难点 2），真实 UA 对付 UA 校验，
        cookie 让登录站点认得出我们是谁。

        Sec-Fetch-* 这几个头也得带：不少 CDN（Cloudflare 之类）会检查它们来
        判断请求到底是不是浏览器发的，缺了就直接回 403。
        """
        headers = {
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
        }
        if referer:
            headers["Referer"] = referer

        if self._user_agent is None:
            try:
                pages = self._context.pages if self._context else []
                if pages:
                    self._user_agent = str(await pages[0].evaluate("() => navigator.userAgent"))
            except Exception:
                log.debug("拿不到浏览器 UA，用 httpx 默认值", exc_info=True)
                self._user_agent = ""
        if self._user_agent:
            headers["User-Agent"] = self._user_agent

        try:
            if self._context is not None:
                cookies = await self._context.cookies(url)
                if cookies:
                    headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        except Exception:
            log.debug("拿不到 cookie，需要登录的资源可能下载失败", exc_info=True)

        return headers

    async def _fetch_via_httpx(
        self, url: str, referer: str, first_status: int | None = None
    ) -> tuple[bytes, str]:
        """兜底下载通道。显式走系统代理，手动带全套浏览器请求头和会话信息。"""
        client = await self._http_client()
        headers = await self._session_headers(url, referer, "image/*,video/*,*/*;q=0.8")
        try:
            resp = await client.get(url, headers=headers)
        except Exception as e:
            proxy_url, source = detect_proxy()
            hint = (
                f"当前使用的代理是 {proxy_url}（来源：{source}），请确认代理软件正在运行"
                if proxy_url
                else "当前没有检测到任何代理设置。如果这个网站需要代理才能访问，"
                     "请先在系统设置里配好代理"
            )
            raise RuntimeError(
                f"两条下载通道都连不上：{type(e).__name__}: {e}。{hint}"
            ) from e
        self._raise_for_status(resp.status_code, both_channels=first_status is not None)
        return resp.content, resp.headers.get("content-type", "")

    async def _stream_to_file(self, item: dict[str, Any], directory: Path) -> SaveResult:
        """
        大文件流式下载（主要是视频），边下边写，不整个读进内存。

        为什么这条路径不走 page.request：playwright 的 APIResponse 只有 body()，
        一次性返回全部字节，没有流式接口。所以大文件改用 httpx，并手动把
        browser context 的 cookie / UA / Referer 搬过来，尽量保持会话一致。

        去重、重名避让、历史记录仍然复用 core.downloader 里的公共步骤，
        保证和小文件路径的行为完全一致。
        """
        try:
            import httpx  # noqa: F401
        except ImportError:
            raise FatalDownloadError("缺少 httpx，无法下载大文件。请执行 pip install -r requirements.txt")

        url = str(item.get("url") or "")
        referer = str(item.get("referrer") or "")
        headers = await self._session_headers(url, referer, "video/*,*/*;q=0.8")

        dedupe = bool(self.cfg.get("save.dedupe_by_md5", True))
        max_len = int(self.cfg.get("save.max_filename_length", 100) or 100)

        client = await self._http_client()
        # timeout=None：整段流不设总时限，大视频下几分钟是正常的
        async with client.stream("GET", url, headers=headers, timeout=None) as resp:
            # 视频只有这一条通道，没得降级，所以直接按最终结果判定
            self._raise_for_status(resp.status_code, both_channels=False)

            content_type = resp.headers.get("content-type", "")
            ext = scanner.guess_extension(url, content_type)
            filename = naming.filename_from_url(url, ext, fallback_stem="video")
            target = await asyncio.to_thread(
                downloader.prepare_target, directory, filename, max_len
            )
            tmp = target.with_name(target.name + downloader.TMP_SUFFIX)

            digest = hashlib.md5()
            size = 0
            try:
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        if self._cancel_download:
                            raise FatalDownloadError("已取消")
                        f.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            except BaseException:
                # 中断时把半截文件清掉，别在你的目录里留垃圾
                tmp.unlink(missing_ok=True)
                raise

        md5 = digest.hexdigest()
        existing = downloader.check_duplicate(self.history, md5, dedupe)
        if existing is not None:
            tmp.unlink(missing_ok=True)
            return SaveResult(ok=True, skipped=True, duplicate_of=existing, md5=md5, size=size)

        os.replace(tmp, target)
        log.info("已保存（流式）%s（%d 字节）", target, size)
        downloader.commit_record(
            self.history, path=target, digest=md5, size=size,
            kind="browser", source=url,
            width=int(item.get("width") or 0), height=int(item.get("height") or 0),
        )
        return SaveResult(ok=True, path=target, md5=md5, size=size)

    # --- 缩略图 -------------------------------------------------------------

    async def _do_thumbnails(self, items: list[dict[str, Any]], generation: int) -> None:
        """
        给扫描结果抓缩略图。

        这是一次额外的请求，但多数情况会命中浏览器自己的缓存（图片刚刚才在
        页面上显示过），所以通常很快。不走 _rate_limit——缩略图请求都是缓存命中，
        排队反而会让界面几分钟都是空白。但并发有独立上限，不会压垮站点。

        只处理前 thumbnail_limit 条；视频、流媒体、DRM 一律跳过。
        """
        try:
            page = await self._active_page()
        except RuntimeError:
            return

        limit = max(0, int(self.cfg.get("browser.thumbnail_limit", 200) or 0))
        concurrency = max(1, int(self.cfg.get("browser.thumbnail_concurrency", 4) or 4))
        sem = asyncio.Semaphore(concurrency)
        targets = list(enumerate(items))[:limit]

        async def one(index: int, item: dict[str, Any]) -> None:
            if generation != self._thumb_generation:
                return
            kind = str(item.get("kind"))
            note = str(item.get("note"))
            if kind == scanner.KIND_VIDEO or note in (scanner.NOTE_STREAM, scanner.NOTE_DRM):
                return
            async with sem:
                if generation != self._thumb_generation:
                    return
                try:
                    data, _ = await self._fetch_bytes(page, item)
                except Exception as e:
                    log.debug("缩略图取不到 %s：%s", str(item.get("url"))[:100], e)
                    return
            if generation == self._thumb_generation and data:
                self.thumbnail_ready.emit(index, bytes(data))

        await asyncio.gather(*(one(i, it) for i, it in targets), return_exceptions=True)
        log.info("缩略图任务结束（第 %d 代，共 %d 条）", generation, len(targets))

    # --- 登录状态 -----------------------------------------------------------

    async def _do_login_domains(self) -> None:
        """
        列出当前受控浏览器里有 cookie 的域名（CLAUDE.md 第 6.4 节）。

        用来让你一眼看出哪些站点已经登录过了。
        """
        if self._context is None:
            self.login_domains.emit([])
            return
        try:
            cookies = await self._context.cookies()
        except Exception as e:
            log.warning("读取 cookie 失败：%s", e)
            self.login_domains.emit([])
            return
        domains = sorted({str(c.get("domain", "")).lstrip(".") for c in cookies if c.get("domain")})
        log.info("受控浏览器中有 cookie 的域名共 %d 个", len(domains))
        self.login_domains.emit(domains)


def clear_profile(profile_dir: Path) -> tuple[bool, str]:
    """
    删除 chrome-profile 目录，清掉所有登录数据（CLAUDE.md 第 6.4 节的按钮）。

    调用前必须让用户二次确认，调用时必须保证受控 Chrome 已经关掉，
    否则文件被占用会删不干净。

    返回 (是否成功, 中文说明)。
    """
    import shutil

    profile_dir = Path(profile_dir)
    if not profile_dir.exists():
        return True, "登录数据本来就是空的"
    try:
        shutil.rmtree(profile_dir)
        log.info("已删除 chrome-profile：%s", profile_dir)
        return True, "已清除全部登录数据，下次启动浏览器需要重新登录"
    except OSError as e:
        log.error("删除 chrome-profile 失败：%s", e)
        return False, (
            f"删除失败：{e}。\n"
            "请先把受控 Chrome 完全关掉（任务管理器里确认没有 chrome.exe 残留），再试一次。"
        )
