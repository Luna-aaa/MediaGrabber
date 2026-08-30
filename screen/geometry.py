"""
Windows 屏幕几何与 DPI 感知。整个项目最容易出错的地方就在这里。

背景：同一个点在 Windows 上有两套坐标，随便混用就会「框选位置偏一点」：

    物理像素   显卡真正输出的像素。mss 截图、最终裁剪用的就是它。
               例：2560 x 1600
    逻辑像素   系统按缩放比例换算后的坐标。Qt 的鼠标事件、窗口几何用的是它。
               例：150% 缩放下是 1707 x 1067

本模块的职责：
  1. 在 QApplication 创建之前，把进程 DPI 感知模式**显式**设成 Per-Monitor V2
  2. 用 Win32 API 拿到每块屏真正的物理矩形（不经过任何缩放换算）
  3. 把 Qt 的 QScreen 和 Win32 的显示器一一对应起来

关于第 1 点为什么重要：Windows 规定进程的 DPI 感知模式**只能设置一次，先到先得**。
mss 在初始化时会调用 SetProcessDPIAware()（老的 system-aware 模式）。如果它抢在
Qt 前面设置成功，Qt 的所有几何计算都会错位，而且不报错、不崩溃，只是悄悄偏一点。
所以我们在最早期主动设成 Per-Monitor V2，把这个位置占掉。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# --- Win32 常量 -------------------------------------------------------------
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1

# DPI_AWARENESS_CONTEXT 句柄值（负数常量，直接当指针用）
DPI_AWARENESS_CONTEXT_UNAWARE = -1
DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

_AWARENESS_NAMES = {
    0: "DPI 无感知（最糟，所有坐标都会被系统偷偷缩放）",
    1: "系统级 DPI 感知（单屏勉强够用，多屏不同缩放时会错）",
    2: "每显示器 DPI 感知（正确）",
}

if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
else:  # 非 Windows 上只是为了让 import 不炸，功能全部不可用
    _user32 = None  # type: ignore[assignment]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),      # 显示器完整矩形（物理像素）
        ("rcWork", RECT),         # 去掉任务栏后的工作区
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),   # 形如 \\.\DISPLAY1
    ]


@dataclass(frozen=True)
class MonitorRect:
    """一块显示器的物理像素矩形。左上角坐标可以是负数（副屏摆在主屏左边时）。"""

    device: str
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def __str__(self) -> str:
        tag = " [主屏]" if self.is_primary else ""
        return f"{self.device} {self.width}x{self.height} @({self.left},{self.top}){tag}"


@dataclass
class ScreenMap:
    """
    一块屏幕的完整坐标信息：Qt 的逻辑视角 + Win32 的物理视角，以及两者的换算系数。

    这是遮罩窗口做坐标换算的唯一依据。
    """

    name: str
    # Qt 逻辑坐标（全局虚拟桌面坐标系）
    lx: int
    ly: int
    lw: int
    lh: int
    # Win32 物理像素坐标
    px: int
    py: int
    pw: int
    ph: int
    # Qt 报告的设备像素比，仅供参考和日志比对
    dpr: float = 1.0
    # 配对方式，用于排查问题：device-name / size / index
    match_method: str = "device-name"
    qscreen: Any = field(default=None, repr=False)

    @property
    def sx(self) -> float:
        """
        水平方向 逻辑->物理 的换算系数。

        故意用 物理宽/逻辑宽 现算，而不是直接用 Qt 报的 dpr：
        150% 缩放下 2560/1.5 = 1706.67，Qt 会把逻辑宽度取整成 1707，
        此时真实系数是 2560/1707 = 1.49971…，跟 1.5 差一点点。
        用现算的系数才能保证「拖到最右边」正好落在第 2560 个像素上。
        """
        return self.pw / self.lw if self.lw else 1.0

    @property
    def sy(self) -> float:
        """垂直方向 逻辑->物理 的换算系数。理由同 sx。"""
        return self.ph / self.lh if self.lh else 1.0

    def to_physical(self, local_lx: float, local_ly: float) -> tuple[int, int]:
        """
        把「本屏窗口内的逻辑坐标」换算成「虚拟桌面物理坐标」。

        参数是相对于本屏左上角的逻辑坐标（遮罩窗口正好铺满一块屏，
        所以 Qt 给的窗口内坐标可以直接传进来）。

        用 round() 而不是 int()：150% 缩放下 int() 会系统性偏小。
        """
        return (
            self.px + round(local_lx * self.sx),
            self.py + round(local_ly * self.sy),
        )

    def clamp_physical(self, x: int, y: int) -> tuple[int, int]:
        """把物理坐标夹进本屏范围内，避免鼠标甩出屏幕导致裁剪越界。"""
        return (
            min(max(x, self.px), self.px + self.pw),
            min(max(y, self.py), self.py + self.ph),
        )

    def describe(self) -> str:
        """一行摘要，用于写日志。排查坐标问题时全靠它。"""
        return (
            f"{self.name}: 逻辑 {self.lw}x{self.lh}@({self.lx},{self.ly}) | "
            f"物理 {self.pw}x{self.ph}@({self.px},{self.py}) | "
            f"Qt报告DPR={self.dpr:.4f} 实算sx={self.sx:.6f} sy={self.sy:.6f} | "
            f"配对方式={self.match_method}"
        )


# ---------------------------------------------------------------------------
# DPI 感知
# ---------------------------------------------------------------------------


def set_per_monitor_v2_awareness() -> str:
    """
    把当前进程的 DPI 感知模式设成 Per-Monitor V2。

    **必须在创建 QApplication 之前、以及 import mss 之前调用。**

    返回：一句中文说明，讲清楚走的是哪条路径，直接写进启动日志。

    三级降级（新系统走第一条，老系统才会掉到后面）：
        1. SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)  Win10 1703+
        2. shcore.SetProcessDpiAwareness(2)                      Win8.1+
        3. user32.SetProcessDPIAware()                           Vista+

    什么情况会「失败」：进程的感知模式已经被设过了（比如清单文件里声明过，
    或者某个库抢先设了）。这时 API 返回失败，我们照样往下走——因为已经设过的
    模式才是真正生效的，调用方应该用 describe_awareness() 去查实际值。
    """
    if not IS_WINDOWS:
        return "非 Windows 平台，跳过 DPI 感知设置"

    try:
        _user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        _user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        ok = _user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        )
        if ok:
            return "已设为 Per-Monitor V2（SetProcessDpiAwarenessContext）"
    except (AttributeError, OSError) as e:
        log.debug("SetProcessDpiAwarenessContext 不可用：%s", e)

    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        hr = shcore.SetProcessDpiAwareness(2)
        if hr == 0:
            return "已设为 Per-Monitor V1（shcore.SetProcessDpiAwareness）"
    except (AttributeError, OSError) as e:
        log.debug("shcore.SetProcessDpiAwareness 不可用：%s", e)

    try:
        if _user32.SetProcessDPIAware():
            return "已设为 系统级 DPI 感知（SetProcessDPIAware，降级路径）"
    except (AttributeError, OSError) as e:
        log.debug("SetProcessDPIAware 不可用：%s", e)

    return "DPI 感知模式设置未生效（很可能是已经被设置过了，请看下一行的实际值）"


def describe_awareness() -> str:
    """
    查询进程**实际生效**的 DPI 感知模式。

    这是判断有没有被 mss 抢跑的唯一可靠方法，启动时必须打进日志。
    正常应该看到「每显示器 DPI 感知」并且带 V2 标记。
    """
    if not IS_WINDOWS:
        return "非 Windows 平台"
    try:
        _user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        ctx = _user32.GetThreadDpiAwarenessContext()

        _user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        _user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        awareness = _user32.GetAwarenessFromDpiAwarenessContext(ctx)

        text = _AWARENESS_NAMES.get(awareness, f"未知值 {awareness}")

        # 再确认一下是不是 V2（V1 和 V2 的 awareness 值都是 2，要单独比对）
        try:
            _user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            _user32.AreDpiAwarenessContextsEqual.restype = wintypes.BOOL
            is_v2 = bool(
                _user32.AreDpiAwarenessContextsEqual(
                    ctypes.c_void_p(ctx),
                    ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2),
                )
            )
            if awareness == 2:
                text += " · V2" if is_v2 else " · V1"
        except (AttributeError, OSError):
            pass
        return text
    except (AttributeError, OSError) as e:
        return f"查询失败：{e}"


def awareness_is_per_monitor() -> bool:
    """进程是否处于每显示器 DPI 感知。env_check 用它来判定通过/不通过。"""
    if not IS_WINDOWS:
        return False
    try:
        _user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        ctx = _user32.GetThreadDpiAwarenessContext()
        _user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        _user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        return _user32.GetAwarenessFromDpiAwarenessContext(ctx) == 2
    except (AttributeError, OSError):
        return False


# ---------------------------------------------------------------------------
# 显示器枚举（物理像素）
# ---------------------------------------------------------------------------


def enum_display_monitors() -> list[MonitorRect]:
    """
    枚举所有显示器的**物理像素**矩形。

    只有在进程已经是 DPI 感知的前提下，这里返回的才是真正的物理像素；
    否则 Windows 会返回缩放后的假坐标。所以调用顺序很重要：
    先 set_per_monitor_v2_awareness()，再调这个。

    返回：按主屏优先、然后按左上角坐标排序的列表。失败时返回空列表并写日志。
    """
    if not IS_WINDOWS:
        return []

    monitors: list[MonitorRect] = []

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )

    def _callback(hmonitor, hdc, lprect, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if _user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            r = info.rcMonitor
            monitors.append(
                MonitorRect(
                    device=info.szDevice,
                    left=r.left,
                    top=r.top,
                    width=r.right - r.left,
                    height=r.bottom - r.top,
                    is_primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                )
            )
        return True  # 继续枚举下一块

    try:
        if not _user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_callback), 0):
            log.error("EnumDisplayMonitors 调用失败，错误码 %s", ctypes.get_last_error())
    except OSError as e:
        log.error("枚举显示器失败：%s", e)
        return []

    monitors.sort(key=lambda m: (not m.is_primary, m.left, m.top))
    return monitors


def virtual_desktop_rect() -> tuple[int, int, int, int]:
    """
    整个虚拟桌面（所有屏拼起来的外接矩形）的物理像素矩形。

    返回 (left, top, width, height)。left/top 可能是负数。
    这个矩形就是 mss 要截的范围。
    """
    if not IS_WINDOWS:
        return (0, 0, 0, 0)
    g = _user32.GetSystemMetrics
    return (
        g(SM_XVIRTUALSCREEN),
        g(SM_YVIRTUALSCREEN),
        g(SM_CXVIRTUALSCREEN),
        g(SM_CYVIRTUALSCREEN),
    )


def monitor_rect_from_hwnd(hwnd: int) -> MonitorRect | None:
    """
    问 Windows：这个窗口现在在哪块显示器上？那块显示器的物理矩形是多少？

    遮罩窗口显示出来之后用它做一次交叉验证——这是最权威的答案，
    如果和我们事先配对的结果不一致，说明配对逻辑有问题，必须写警告日志。

    什么情况会失败：hwnd 无效（窗口还没创建）时返回 None。
    """
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        _user32.MonitorFromWindow.restype = wintypes.HMONITOR
        hmon = _user32.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return None
        r = info.rcMonitor
        return MonitorRect(
            device=info.szDevice,
            left=r.left,
            top=r.top,
            width=r.right - r.left,
            height=r.bottom - r.top,
            is_primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
        )
    except (AttributeError, OSError) as e:
        log.warning("MonitorFromWindow 失败：%s", e)
        return None


# ---------------------------------------------------------------------------
# Qt 屏幕 <-> Win32 显示器 配对
# ---------------------------------------------------------------------------


def map_screens(qscreens: list) -> list[ScreenMap]:
    """
    把 Qt 的每个 QScreen 和 Win32 枚举出来的物理显示器配对。

    参数：qscreens —— QGuiApplication.screens() 的返回值

    返回：ScreenMap 列表，顺序和 qscreens 一致。

    配对策略（依次尝试，全部会写进日志）：
        1. 设备名匹配 —— QScreen.name() 在 Windows 上就是 \\\\.\\DISPLAY1，
           和 MONITORINFOEXW.szDevice 完全一致，这是最可靠的
        2. 尺寸匹配   —— 按 round(逻辑宽 x dpr) == 物理宽 找唯一匹配
        3. 索引兜底   —— 前两条都不行时按顺序硬配，并写 WARNING

    什么情况会失败：拿不到任何 Win32 显示器信息时，退化成「物理 = 逻辑 x dpr」
    自行推算，同样写 WARNING。这种情况下坐标可能不准，日志里会明确写出来。
    """
    monitors = enum_display_monitors()
    used: set[int] = set()
    result: list[ScreenMap] = []

    by_device = {m.device: i for i, m in enumerate(monitors)}

    for idx, qs in enumerate(qscreens):
        geo = qs.geometry()
        lx, ly, lw, lh = geo.x(), geo.y(), geo.width(), geo.height()
        dpr = float(qs.devicePixelRatio())
        name = qs.name() or f"screen-{idx}"

        chosen: int | None = None
        method = ""

        # 策略 1：设备名
        j = by_device.get(name)
        if j is not None and j not in used:
            chosen, method = j, "device-name"

        # 策略 2：尺寸唯一匹配
        if chosen is None and monitors:
            want_w, want_h = round(lw * dpr), round(lh * dpr)
            candidates = [
                i
                for i, m in enumerate(monitors)
                if i not in used and abs(m.width - want_w) <= 1 and abs(m.height - want_h) <= 1
            ]
            if len(candidates) == 1:
                chosen, method = candidates[0], "size"

        # 策略 3：按索引硬配
        if chosen is None and idx < len(monitors) and idx not in used:
            chosen, method = idx, "index"
            log.warning(
                "屏幕 %s 无法按设备名或尺寸配对，退化为按索引配对。"
                "如果框选位置不准，请把这条日志发给开发者。",
                name,
            )

        if chosen is not None:
            m = monitors[chosen]
            used.add(chosen)
            sm = ScreenMap(
                name=name,
                lx=lx, ly=ly, lw=lw, lh=lh,
                px=m.left, py=m.top, pw=m.width, ph=m.height,
                dpr=dpr, match_method=method, qscreen=qs,
            )
        else:
            # 完全拿不到 Win32 信息，只能自己推算
            log.warning(
                "屏幕 %s 找不到对应的 Win32 显示器，改用 逻辑x%.4f 推算物理坐标，"
                "坐标可能有 1~2 像素误差。",
                name, dpr,
            )
            sm = ScreenMap(
                name=name,
                lx=lx, ly=ly, lw=lw, lh=lh,
                px=round(lx * dpr), py=round(ly * dpr),
                pw=round(lw * dpr), ph=round(lh * dpr),
                dpr=dpr, match_method="fallback-dpr", qscreen=qs,
            )

        result.append(sm)

    return result
