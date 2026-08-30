"""
屏幕抓取：把整个虚拟桌面抓成一张物理像素的 QImage。

这是「画面冻结」的实现基础（CLAUDE.md 难点 6）。顺序绝对不能反：
    先抓图  ->  再显示遮罩窗口
反过来的话，遮罩窗口本身会被抓进画面里。

关于 mss 与 DPI 的坑（难点 5）：
mss 在初始化时会调用 SetProcessDPIAware()。我们在 main.py 里已经抢先把进程设成了
Per-Monitor V2，所以 mss 这次调用会失败——这是**预期行为**，无害。
但为了保险，这个模块只在函数内部 import mss，绝不在模块顶层 import，
这样即使有人搞错了 main.py 的顺序，也不会因为 import 就把 DPI 模式定死。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from screen import geometry

log = logging.getLogger(__name__)


def grab_virtual_desktop() -> tuple[QImage, tuple[int, int, int, int]]:
    """
    抓取整个虚拟桌面（所有显示器拼起来的矩形）。

    返回：
        (QImage, (virt_left, virt_top, virt_width, virt_height))
        QImage 的尺寸就是虚拟桌面的物理像素尺寸；
        第二项是虚拟桌面左上角的物理坐标，可能是负数（副屏在主屏左边时）。
        后面裁剪时要用「物理坐标 - virt_left/top」换算成图内坐标。

    什么情况会失败：
        - mss 没装        -> ImportError，提示去装依赖
        - 抓图被系统拒绝  -> RuntimeError（远程桌面断开、锁屏、显卡驱动异常时会出现）
        - 虚拟桌面尺寸为 0 -> RuntimeError（显示器全部断开时）
    """
    if not geometry.awareness_is_per_monitor():
        # 走到这说明 main.py 的初始化顺序被破坏了，坐标一定会偏，必须留下证据
        log.error(
            "进程不是「每显示器 DPI 感知」，当前是：%s。"
            "截图坐标很可能不准确，请检查 main.py 里是否在创建 QApplication 之前"
            "调用了 geometry.set_per_monitor_v2_awareness()。",
            geometry.describe_awareness(),
        )

    vx, vy, vw, vh = geometry.virtual_desktop_rect()
    if vw <= 0 or vh <= 0:
        raise RuntimeError(
            f"虚拟桌面尺寸异常（{vw}x{vh}），可能所有显示器都已断开或系统正在切换显示模式。"
        )

    try:
        import mss  # 故意延迟到这里 import，理由见模块开头的说明
    except ImportError as e:
        raise ImportError(
            "缺少 mss 库，无法截图。请在项目目录下执行：pip install -r requirements.txt"
        ) from e

    region = {"left": vx, "top": vy, "width": vw, "height": vh}
    try:
        with mss.mss() as sct:
            shot = sct.grab(region)
    except Exception as e:
        raise RuntimeError(
            f"截图失败：{e}。常见原因是屏幕处于锁定状态、远程桌面已断开，"
            f"或显卡驱动刚刚重启。"
        ) from e

    # mss 给的是 BGRA 原始字节。
    # 用 Format_RGB32 而不是 Format_ARGB32：mss 截出来的 alpha 通道全是 0，
    # 按 ARGB32 解释会得到一张完全透明的图。RGB32 会忽略 alpha，正好。
    # 小端机器上 RGB32 在内存里的字节序就是 B,G,R,X，和 mss 的 BGRA 完全对齐。
    image = QImage(
        shot.bgra,
        shot.width,
        shot.height,
        shot.width * 4,
        QImage.Format.Format_RGB32,
    ).copy()  # copy() 做深拷贝，脱离 mss 的缓冲区，否则 with 块结束后数据会失效

    if image.isNull():
        raise RuntimeError("截图数据转换为图像失败（QImage 为空）")

    log.debug("已冻结画面：%dx%d，虚拟桌面原点 (%d,%d)", image.width(), image.height(), vx, vy)
    return image, (vx, vy, vw, vh)


def qimage_to_png_bytes(image: QImage) -> bytes:
    """
    把 QImage 编码成 PNG 字节。

    为什么不直接 image.save(路径)：所有落盘都要走 core.downloader.save_bytes()，
    才能享受统一的 MD5 去重、重名避让、原子写入。所以这里先转成字节。

    什么情况会失败：图像为空或编码失败时抛 RuntimeError。
    """
    if image.isNull():
        raise RuntimeError("图像为空，无法编码为 PNG")

    buf = QBuffer()
    if not buf.open(QIODevice.OpenModeFlag.WriteOnly):
        raise RuntimeError("无法打开内存缓冲区")
    try:
        if not image.save(buf, "PNG"):
            raise RuntimeError("PNG 编码失败（可能是图像尺寸异常或内存不足）")
        return bytes(buf.data())
    finally:
        buf.close()
