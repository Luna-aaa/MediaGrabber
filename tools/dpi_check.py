"""
DPI 坐标标定工具。用来严格验证「框出来的范围」和「存下来的图」是不是完全一致。

为什么需要它：150% 缩放下，1 个逻辑像素 = 1.5 个物理像素，靠肉眼根本看不出
差了一两个像素。这个工具用「已知内容的标定图 + 自动比对」把误差量化出来。

===========================================================================
 用法
===========================================================================
 第 1 步  显示标定图（这个窗口会铺满屏幕，同时把一份一模一样的参考图存到
          tools/_dpi_reference_*.png）：

              python tools/dpi_check.py

 第 2 步  保持这个窗口开着，切到 MediaGrabber，按 Ctrl+Alt+S，
          然后按 A 键（整屏捕获）。

 第 3 步  回到这里按 Esc 关掉标定图，然后比对：

              python tools/dpi_check.py --verify "D:\\MediaGrabber\\Screen\\<日期>\\screen_xxx.png"

          它会告诉你尺寸对不对、有没有偏移、缩放系数准不准。
===========================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from screen import geometry  # noqa: E402

_DPI_SET_RESULT = geometry.set_per_monitor_v2_awareness()

# 三个纯色标记块的颜色和位置（位置相对于屏幕物理像素）
MARKERS = [
    ("红", (255, 0, 0), "left-top"),
    ("绿", (0, 255, 0), "right-top"),
    ("蓝", (0, 0, 255), "left-bottom"),
]
MARKER_SIZE = 200
MARKER_INSET = 300


def _marker_rects(width: int, height: int) -> list[tuple[str, tuple[int, int, int], tuple[int, int, int, int]]]:
    """算出三个标记块在这个尺寸下的矩形 (x, y, w, h)。"""
    s = MARKER_SIZE
    i = MARKER_INSET
    positions = {
        "left-top": (i, i),
        "right-top": (width - i - s, i),
        "left-bottom": (i, height - i - s),
    }
    out = []
    for name, color, key in MARKERS:
        x, y = positions[key]
        out.append((name, color, (x, y, s, s)))
    return out


# ===========================================================================
# 生成 + 显示标定图
# ===========================================================================


def build_pattern(width: int, height: int, label: str):
    """
    画一张标定图，尺寸就是屏幕的物理像素尺寸。

    图上有：
      - 100 像素一格的浅灰网格，500 像素一格的深灰网格 + 坐标数字
      - 三个 200x200 的纯色标记块，位置精确已知（比对时靠它们量偏移）
      - 沿最外圈的 1 像素黑框（用来检查边缘有没有被切掉或多出黑边）

    返回 QImage（Format_RGB32）。
    """
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen

    img = QImage(width, height, QImage.Format.Format_RGB32)
    img.fill(QColor(255, 255, 255))

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    # 细网格
    p.setPen(QPen(QColor(225, 225, 225), 1))
    for x in range(0, width, 100):
        p.drawLine(x, 0, x, height)
    for y in range(0, height, 100):
        p.drawLine(0, y, width, y)

    # 粗网格 + 坐标数字
    font = QFont()
    font.setPixelSize(16)
    p.setFont(font)
    p.setPen(QPen(QColor(160, 160, 160), 1))
    for x in range(0, width, 500):
        p.drawLine(x, 0, x, height)
    for y in range(0, height, 500):
        p.drawLine(0, y, width, y)
    p.setPen(QPen(QColor(90, 90, 90), 1))
    for x in range(0, width, 500):
        p.drawText(x + 4, 20, str(x))
    for y in range(0, height, 500):
        p.drawText(4, y + 20, str(y))

    # 三个标记块，块内写上自己的精确坐标
    for name, color, (mx, my, mw, mh) in _marker_rects(width, height):
        p.fillRect(QRect(mx, my, mw, mh), QColor(*color))
        p.setPen(QPen(QColor(255, 255, 255)))
        f2 = QFont()
        f2.setPixelSize(18)
        f2.setBold(True)
        p.setFont(f2)
        p.drawText(
            QRect(mx, my, mw, mh),
            Qt.AlignmentFlag.AlignCenter,
            f"{name}\n({mx},{my})\n{mw}x{mh}",
        )

    # 最外圈 1 像素黑框
    p.setPen(QPen(QColor(0, 0, 0), 1))
    p.drawRect(0, 0, width - 1, height - 1)

    # 中间的说明文字
    info = QFont()
    info.setPixelSize(30)
    p.setFont(info)
    p.setPen(QPen(QColor(30, 30, 30)))
    text = (
        f"{label}\n"
        f"物理分辨率 {width} x {height}\n\n"
        f"按 Ctrl+Alt+S 唤起框选，然后按 A 键整屏捕获\n"
        f"完成后按 Esc 关掉本窗口，再运行：\n"
        f"python tools/dpi_check.py --verify 刚才存下来的图片路径"
    )
    p.drawText(
        QRect(0, height // 2 - 200, width, 400),
        Qt.AlignmentFlag.AlignCenter,
        text,
    )
    p.end()
    return img


def _make_pattern_widget(smap, image):
    """
    把标定图严格 1:1 铺满一块屏幕。

    这里必须做到「1 个图像像素 = 1 个屏幕物理像素」，否则整个标定就没意义了——
    屏幕上显示的图本身就是歪的，截图再准也比对不出正确结果。

    关键在 paintEvent：QPainter 默认工作在逻辑坐标系，落到画布上要乘以 Qt 的
    devicePixelRatio（你的机器是 1.5）。直接画物理像素的图会被重采样，
    150% 这种非整数缩放下会错开半个像素、标记块尺寸会少 1 像素。
    先把画笔缩放 1/dpr 切进设备像素坐标系，才能严格对齐。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtWidgets import QWidget

    class _PatternWidget(QWidget):
        def __init__(self):
            super().__init__()
            # DPR 保持默认的 1.0，绘制时靠画笔变换来对齐
            self._pixmap = QPixmap.fromImage(image)
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
            )
            self.setGeometry(smap.lx, smap.ly, smap.lw, smap.lh)

        def paintEvent(self, event):
            painter = QPainter(self)
            dpr = self.devicePixelRatioF() or 1.0
            painter.scale(1.0 / dpr, 1.0 / dpr)
            painter.drawPixmap(0, 0, self._pixmap)

    return _PatternWidget()


def run_display() -> int:
    """生成标定图、存成参考文件、铺满每块屏显示。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    if not geometry.awareness_is_per_monitor():
        print(f"[  警告  ] DPI 感知模式不对：{geometry.describe_awareness()}")
        print("           标定结果不可信，请先解决这个问题。")

    maps = geometry.map_screens(QGuiApplication.screens())
    windows = []
    out_dir = Path(__file__).resolve().parent

    print("=" * 70)
    print("  DPI 标定图")
    print("=" * 70)
    print(f"DPI 设置：{_DPI_SET_RESULT}")
    print(f"实际生效：{geometry.describe_awareness()}")
    print()

    for sm in maps:
        print(f"  {sm.describe()}")
        image = build_pattern(sm.pw, sm.ph, sm.name)
        safe = "".join(c if c.isalnum() else "_" for c in sm.name).strip("_") or "screen"
        ref_path = out_dir / f"_dpi_reference_{safe}.png"
        if image.save(str(ref_path), "PNG"):
            print(f"    参考图已保存：{ref_path}")
        else:
            print(f"    [失败] 参考图保存失败：{ref_path}")
        windows.append(_make_pattern_widget(sm, image))

    for w in windows:
        w.show()
        w.raise_()
    if windows:
        windows[0].activateWindow()

    print()
    print("现在：按 Ctrl+Alt+S 唤起 MediaGrabber 框选，再按 A 键整屏捕获。")
    print("完成后点一下这个标定窗口再按 Esc 关闭它。")
    print()

    # Esc 关闭
    from PySide6.QtGui import QShortcut, QKeySequence

    for w in windows:
        sc = QShortcut(QKeySequence(Qt.Key.Key_Escape), w)
        sc.activated.connect(app.quit)

    return app.exec()


# ===========================================================================
# 比对
# ===========================================================================


def find_marker_bbox(im, color, tol: int = 40):
    """
    在图里找出某个纯色标记块的外接矩形。

    参数：
        im    —— PIL Image（RGB 模式）
        color —— 目标颜色 (r, g, b)
        tol   —— 每个通道允许的误差

    返回：(left, top, right, bottom)，找不到返回 None。
    """
    from PIL import ImageChops

    r, g, b = im.split()
    mr = r.point(lambda v: 255 if abs(v - color[0]) <= tol else 0)
    mg = g.point(lambda v: 255 if abs(v - color[1]) <= tol else 0)
    mb = b.point(lambda v: 255 if abs(v - color[2]) <= tol else 0)
    mask = ImageChops.multiply(ImageChops.multiply(mr, mg), mb)
    return mask.getbbox()


def find_latest_capture() -> Path | None:
    """
    自动找出今天最后存的那张截图。

    这样比对时就不用手动去复制文件路径了。找不到返回 None。
    """
    try:
        from config import CONFIG
        from core import naming
    except Exception:
        return None
    try:
        directory = naming.build_save_dir(
            CONFIG.save_root(), "screen",
            screen_subdir=str(CONFIG.get("save.screen_subdir", "Screen")),
            split_by_date=bool(CONFIG.get("save.split_by_date", True)),
        )
        pngs = [p for p in directory.glob("*.png") if p.is_file()]
        if not pngs:
            return None
        return max(pngs, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


def run_verify(saved_path: str | None) -> int:
    """把截下来的图和参考图逐项比对，输出结论。"""
    try:
        from PIL import Image
    except ImportError:
        print("[失败] 缺少 Pillow，无法比对。请执行 pip install -r requirements.txt")
        return 1

    if saved_path:
        saved_file = Path(saved_path)
    else:
        found = find_latest_capture()
        if found is None:
            print("[失败] 在今天的保存目录里没找到任何截图。")
            print("       请先按 Ctrl+Alt+S 截一张，再回来比对。")
            return 1
        saved_file = found
        print(f"（自动选中了今天最新的一张截图：{saved_file.name}）")
        print()

    if not saved_file.exists():
        print(f"[失败] 找不到文件：{saved_file}")
        return 1

    out_dir = Path(__file__).resolve().parent
    refs = sorted(out_dir.glob("_dpi_reference_*.png"))
    if not refs:
        print("[失败] 找不到参考图。请先运行：python tools/dpi_check.py")
        return 1

    saved = Image.open(saved_file).convert("RGB")
    print("=" * 70)
    print("  DPI 坐标比对")
    print("=" * 70)
    print(f"待检文件：{saved_file}")
    print(f"待检尺寸：{saved.width} x {saved.height}")
    print()

    # 挑一张参考图：优先尺寸完全一致的，否则用第一张
    reference_file = next(
        (p for p in refs if Image.open(p).size == saved.size), refs[0]
    )
    ref = Image.open(reference_file).convert("RGB")
    print(f"参考文件：{reference_file}")
    print(f"参考尺寸：{ref.width} x {ref.height}")
    print()

    problems = 0

    # --- 1. 尺寸 -----------------------------------------------------------
    if saved.size == ref.size:
        print(f"[  通过  ] 尺寸完全一致：{saved.width} x {saved.height}")
    else:
        print(f"[  注意  ] 尺寸不一致：截图 {saved.size}，参考 {ref.size}")
        print("           如果你是整屏捕获（按 A 键），这里必须一致，不一致就是缩放系数错了。")
        print("           如果你是手动拖框，尺寸不同是正常的，继续看下面的标记比对。")

    # --- 2. 标记块位移 ------------------------------------------------------
    print()
    deltas = []
    for name, color, _rect in _marker_rects(ref.width, ref.height):
        ref_box = find_marker_bbox(ref, color)
        saved_box = find_marker_bbox(saved, color)
        if ref_box is None:
            print(f"[  失败  ] 参考图里找不到{name}色标记，参考图可能坏了")
            problems += 1
            continue
        if saved_box is None:
            print(f"[  信息  ] 截图里没有{name}色标记（框选范围没盖到它，正常）")
            continue
        dx = saved_box[0] - ref_box[0]
        dy = saved_box[1] - ref_box[1]
        rw = ref_box[2] - ref_box[0]
        rh = ref_box[3] - ref_box[1]
        sw = saved_box[2] - saved_box[0]
        sh = saved_box[3] - saved_box[1]
        deltas.append((name, dx, dy, rw, rh, sw, sh))
        size_note = "" if (rw, rh) == (sw, sh) else f"  <-- 尺寸变了！参考 {rw}x{rh}，截图 {sw}x{sh}"
        print(f"[  信息  ] {name}色标记：位移 ({dx:+d}, {dy:+d})  尺寸 {sw}x{sh}{size_note}")

    print()
    if not deltas:
        print("[  失败  ] 截图里一个标记都没找到，无法判断。请确认你截的是标定图。")
        return 1

    # --- 3. 结论 ------------------------------------------------------------
    dxs = {d[1] for d in deltas}
    dys = {d[2] for d in deltas}
    size_ok = all((d[3], d[4]) == (d[5], d[6]) for d in deltas)

    if len(dxs) == 1 and len(dys) == 1 and size_ok:
        dx, dy = deltas[0][1], deltas[0][2]
        if dx == 0 and dy == 0 and saved.size == ref.size:
            print("[  通过  ] 完美：尺寸、位置、缩放全部精确一致。")
            print("           坐标换算没有任何问题。")
        else:
            print(f"[  通过  ] 缩放系数正确（三个标记位移完全一致，标记尺寸也没变）。")
            print(f"           截图对应的是标定图上从 ({-dx}, {-dy}) 开始的区域。")
            print(f"           如果这跟你框的范围一致，说明坐标完全准确。")
    else:
        problems += 1
        print("[  失败  ] 缩放系数有问题！")
        if len(dxs) > 1 or len(dys) > 1:
            print(f"           三个标记的位移不一致：dx={sorted(dxs)}  dy={sorted(dys)}")
            print("           位移不一致 = 图被拉伸或压缩了，说明逻辑->物理的换算系数算错了。")
        if not size_ok:
            print("           标记块本身的尺寸变了，同样说明发生了缩放。")
        print("           请把 logs 目录下当天的日志发给开发者。")

    print("=" * 70)
    return 1 if problems else 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("--verify", "-v"):
        # 不给路径就自动用今天最新的那张截图
        return run_verify(args[1] if len(args) > 1 else None)
    return run_display()


if __name__ == "__main__":
    sys.exit(main())
