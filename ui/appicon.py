"""
程序图标（一台蓝色小相机），用代码画出来，不依赖任何外部图片文件。

为什么单独抽成一个模块：托盘图标和 exe 图标必须是同一个东西。
分两处各画一遍的话，改了一处忘了另一处，任务栏和托盘区就会出现两个不同的图标。

这个模块只依赖 QtGui，导入它不会有任何副作用（不碰配置、不碰日志、不设 DPI），
所以 tools/make_icon.py 可以放心导入它来生成 .ico 文件。
"""

from __future__ import annotations

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap

# exe 图标里要放哪几个尺寸。Windows 在不同地方用不同尺寸：
# 任务栏 32，桌面大图标 48，资源管理器超大图标 256。
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw_camera_pixmap(size: int) -> QPixmap:
    """
    画一台小相机，边长 size 像素，背景透明。

    参数：size —— 边长（像素）

    所有坐标都按 64 为基准等比缩放，所以任何尺寸下比例都一致。
    """
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
    return pm


def make_app_icon() -> QIcon:
    """
    多尺寸图标，给托盘和窗口用。

    画好几个尺寸而不是只画一个再缩放：小尺寸下缩放会糊，
    按目标尺寸重画则每一档都是清晰的。
    """
    icon = QIcon()
    for size in (16, 24, 32, 48, 64):
        icon.addPixmap(draw_camera_pixmap(size))
    return icon


def pixmap_to_png_bytes(pm: QPixmap) -> bytes:
    """把 QPixmap 编码成 PNG 字节。生成 .ico 时要经过这一步交给 Pillow。"""
    buf = QBuffer()
    if not buf.open(QIODevice.OpenModeFlag.WriteOnly):
        raise RuntimeError("无法打开内存缓冲区")
    try:
        if not pm.save(buf, "PNG"):
            raise RuntimeError("PNG 编码失败")
        return bytes(buf.data())
    finally:
        buf.close()


def write_ico(path) -> None:
    """
    生成 exe 用的 .ico 文件（多尺寸打包在一个文件里）。

    参数：path —— 输出路径

    什么情况会失败：
        - Pillow 没装 -> ImportError
        - 目录不可写   -> OSError

    为什么用 Pillow 而不是 Qt 直接存：Qt 存出来的 ICO 只有单一尺寸，
    Windows 在需要 256x256 的地方（资源管理器的超大图标）就只能拿小图放大，
    会很糊。Pillow 能把所有尺寸打包进同一个 .ico。
    """
    import io

    from PIL import Image

    frames = []
    for size in ICO_SIZES:
        png = pixmap_to_png_bytes(draw_camera_pixmap(size))
        frames.append(Image.open(io.BytesIO(png)).convert("RGBA"))

    # 用最大的那张当主图，其余尺寸通过 sizes 参数一起写进去
    frames[-1].save(path, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
