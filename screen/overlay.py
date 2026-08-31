"""
全屏框选遮罩。

工作流程：
    1. 先用 mss 把整个虚拟桌面抓成一张物理像素的图（画面冻结，难点 6）
    2. 每块屏建一个无边框置顶窗口，铺满该屏，显示属于自己那块的冻结画面
    3. 用户拖框 -> 把 Qt 的逻辑坐标换算成物理坐标（难点 5）
    4. 从第 1 步那张物理像素的图上裁剪，得到与屏幕像素一一对应的结果

为什么每块屏一个窗口，而不是用一个大窗口盖住所有屏：
一个窗口只有一个 devicePixelRatio。跨越两块不同缩放比例的屏时，Qt 会用窗口当前
所在屏的 DPR 去缩放整个窗口内容，另一块屏上的部分必然错位。每屏一窗是标准解法。

两个子模式（Tab 切换）：
    手动（默认）—— 拖框，松手即存，一次一张
    自动        —— 鼠标指哪就分析哪块区域，滚轮换大小，点击选中，Enter 批量存

自动模式为什么是「指哪算哪」而不是「一进来就把所有图框出来」：
第一版就是后者，实测漏检误检都很多，原因见 detector.py 开头的说明。
核心问题是没有鼠标位置当线索时，程序只能靠阈值猜「这块是不是图」，
而一套阈值不可能适配整个屏幕。

自动模式下**依然可以手动补框**。识别必然有失手的时候（CLAUDE.md 难点 7），
不留人工兜底的话，漏掉的那张就永远拿不到了。

坐标的存放约定（改这个文件前务必看懂）：
    候选框在内存里一律存**本屏物理坐标**（识别就是在物理像素的画面上做的）。
    只有绘制的那一刻才换算成逻辑坐标。保存时直接用物理坐标，
    不经过「物理->逻辑->物理」这种来回换算，一个像素都不会丢。
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import replace

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QRect,
    QRectF,
    QRunnable,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QWidget

from screen import capture, geometry
from screen.geometry import ScreenMap

log = logging.getLogger(__name__)

# 两种子模式的名字。config 里的 screen.default_submode 用的也是这两个值。
SUBMODE_MANUAL = "manual"
SUBMODE_AUTO = "auto"

# 提示条文案。放在这里方便以后改。
_HINT_MANUAL = "拖动鼠标框选  ·  Tab = 自动识别  ·  A = 整屏  ·  右键取消当前框  ·  Esc 退出"
_HINT_AUTO = (
    "移动鼠标预览  ·  滚轮换大小  ·  点击选中  ·  也可以自己拖框  ·  "
    "Enter 保存  ·  Backspace 撤销  ·  Tab 回手动  ·  Esc 退出"
)

# 预览框（鼠标底下那块，还没选中）和已选中框的颜色
_COLOR_PREVIEW = QColor("#FACC15")     # 黄
_COLOR_SELECTED = QColor("#22C55E")    # 绿

# 鼠标按下到松开之间移动不超过这么多逻辑像素，就当成「点击」而不是「拖框」。
# 太小的话手一抖就变成画了个 2 像素的框，太大又会让人觉得拖不动。
_CLICK_SLOP_PX = 5

# 鼠标移动不到这么多物理像素就不重算候选。
# 一次查询约 7 毫秒，鼠标每动一个像素都算的话会明显发涩；
# 而在同一张图上小幅移动，结果本来也不会变。
_PROBE_STEP_PX = 6


class OverlayWindow(QWidget):
    """
    盖住**一块**屏幕的遮罩窗口。

    发出的信号：
        region_selected(QRect)     —— 手动框好了一个，立刻存。【虚拟桌面物理像素】坐标
        cancelled()                —— 用户按了 Esc，整个遮罩都该关掉
        detect_requested()         —— 切到了自动模式，请求识别本屏（控制器去跑）
        submode_toggle_requested() —— 按了 Tab
        commit_requested()         —— 按了 Enter，要保存选中的那些

    后三个信号都只是「提出请求」，真正的动作由 OverlayController 做。
    原因：Tab 要同时切换所有屏的模式，Enter 要把所有屏上选中的框一起保存——
    这些都是跨窗口的事，单个窗口没资格自己决定。
    """

    region_selected = Signal(object)
    cancelled = Signal()
    detect_requested = Signal()
    submode_toggle_requested = Signal()
    commit_requested = Signal()

    def __init__(self, smap: ScreenMap, pixmap: QPixmap, cfg, parent=None):
        super().__init__(parent)
        self._smap = smap
        self._pixmap = pixmap
        self._cfg = cfg

        self._origin: QPointF | None = None    # 按下时的窗口内逻辑坐标
        self._current: QPointF | None = None   # 当前鼠标的窗口内逻辑坐标
        self._dragging = False

        # --- 自动模式的状态 -------------------------------------------------
        self._submode = SUBMODE_MANUAL
        # 预计算好的候选索引（detector.RegionIndex）
        self._index = None
        # 鼠标当前位置能选出来的那一串候选，从小到大。本屏**物理**坐标。
        self._levels: list[QRect] = []
        # 当前停在第几层（滚轮改这个）
        self._level = 0
        # 上一次查询用的鼠标物理坐标，用来判断要不要重算
        self._last_probe: tuple[int, int] | None = None
        # 已经点选确认的框，本屏物理坐标。顺序就是保存的顺序。
        self._picks: list[QRect] = []
        # 对应的 _picks 里哪些是你自己拖出来的（只影响显示颜色）
        self._pick_manual: list[bool] = []
        # 识别进行到哪一步了："idle" / "running" / "done" / "failed"
        self._detect_state = "idle"
        self._detect_error = ""

        try:
            self._mask_alpha = int(cfg.get("screen.mask_opacity", 120))
        except (TypeError, ValueError):
            self._mask_alpha = 120
        self._mask_alpha = max(0, min(255, self._mask_alpha))
        self._sel_color = QColor(str(cfg.get("screen.selection_color", "#3B82F6")))
        if not self._sel_color.isValid():
            self._sel_color = QColor("#3B82F6")
        self._min_px = max(1, int(cfg.get("screen.min_selection_px", 4) or 4))
        self._snap = max(0, int(cfg.get("screen.edge_snap_px", 2) or 0))

        # Tool：不在任务栏留按钮。FramelessWindowHint + 精确几何 = 铺满这块屏。
        # 故意不用 showFullScreen()：多屏环境下它经常跑到错误的屏幕上。
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(smap.lx, smap.ly, smap.lw, smap.lh)

    # --- 显示后的交叉验证 ---------------------------------------------------

    @property
    def smap(self) -> ScreenMap:
        """本窗口当前使用的坐标映射。"""
        return self._smap

    def verify_monitor(self) -> geometry.MonitorRect | None:
        """
        窗口显示出来之后，直接问 Windows「我现在在哪块显示器上」。

        这是最权威的答案——它不依赖任何配对猜测。geometry.map_screens() 的配对
        （设备名 / 尺寸 / 索引）只是初始猜测，这里拿到的才是事实。

        返回：Windows 报告的物理矩形；拿不到时返回 None（此时沿用初始猜测）。
        调用方负责在两者不一致时用 apply_screen_map() 纠正过来。
        """
        try:
            hwnd = int(self.winId())
        except (RuntimeError, TypeError) as e:
            log.warning("拿不到窗口句柄，跳过显示器交叉验证：%s", e)
            return None

        actual = geometry.monitor_rect_from_hwnd(hwnd)
        if actual is None:
            log.warning("MonitorFromWindow 没返回结果，%s 沿用初始配对结果", self._smap.name)
            return None

        expected = (self._smap.px, self._smap.py, self._smap.pw, self._smap.ph)
        got = (actual.left, actual.top, actual.width, actual.height)
        if expected == got:
            log.info("屏幕 %s 交叉验证通过：物理矩形 %s", self._smap.name, got)
        else:
            log.warning(
                "屏幕 %s 的配对结果和实际不符：配对得到 %s，Windows 实际报告 %s（设备 %s）。"
                "以 Windows 的结果为准，正在自动纠正。",
                self._smap.name, expected, got, actual.device,
            )
        return actual

    def apply_screen_map(self, smap: ScreenMap, pixmap: QPixmap) -> None:
        """
        换掉本窗口的坐标映射和背景图。

        只在 verify_monitor() 发现配对错了的时候调用，用来把坐标纠正过来。
        """
        self._smap = smap
        self._pixmap = pixmap
        self._reset_selection()
        log.info("屏幕映射已纠正为：%s", smap.describe())

    # --- 绘制 ---------------------------------------------------------------

    def _draw_frozen(self, painter: QPainter) -> None:
        """
        把冻结画面按 1 图像像素 = 1 屏幕物理像素 画出来。

        为什么要绕这一道：QPainter 默认工作在**逻辑**坐标系，落到画布上会乘以
        Qt 的 devicePixelRatio（你的机器是 1.5）。而我们的图是**物理**像素的，
        直接画会被重采样——150% 这种非整数缩放下会糊掉、并且错开半个像素。
        先把画笔缩放 1/dpr 切进设备像素坐标系，图像就能严格 1:1 落上去。
        """
        dpr = self.devicePixelRatioF() or 1.0
        painter.save()
        painter.scale(1.0 / dpr, 1.0 / dpr)
        painter.drawPixmap(0, 0, self._pixmap)
        painter.restore()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        self._draw_frozen(painter)
        # 整体压暗
        painter.fillRect(self.rect(), QColor(0, 0, 0, self._mask_alpha))

        # 自动模式的选中框和预览框，画在正在拖的框下面
        if self._submode == SUBMODE_AUTO:
            self._draw_auto(painter)

        sel = self._selection_rect()
        if sel is not None and sel.width() >= 1 and sel.height() >= 1:
            # 选区内恢复成原始亮度
            painter.save()
            painter.setClipRect(sel)
            self._draw_frozen(painter)
            painter.restore()

            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(self._sel_color, 1))
            painter.drawRect(sel)
            self._draw_size_label(painter, sel)
        else:
            self._draw_hint(painter)

    # --- 自动模式的绘制 -----------------------------------------------------

    def _local_rectf(self, phys: QRect) -> QRectF:
        """把候选框（本屏物理坐标）换算成窗口内逻辑坐标，只用于绘制和命中判断。"""
        x0, y0 = self._smap.to_local_logical(phys.x(), phys.y())
        x1, y1 = self._smap.to_local_logical(phys.x() + phys.width(), phys.y() + phys.height())
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    def _draw_auto(self, painter: QPainter) -> None:
        """
        画自动模式的两样东西：已经选中的框（绿实线）+ 鼠标底下的预览框（黄虚线）。

        已选中的框内部会恢复成原始亮度，让你一眼看清「我到底选了哪几块」。
        预览框不恢复亮度——它还只是个候选，不该看起来像已经选上了。
        """
        # 先统一把选中区域的亮度恢复，再画线，否则线会被后画的亮块盖住
        for phys in self._picks:
            painter.save()
            painter.setClipRect(self._local_rectf(phys).toRect())
            self._draw_frozen(painter)
            painter.restore()

        painter.setBrush(Qt.BrushStyle.NoBrush)
        for idx, phys in enumerate(self._picks):
            manual = idx < len(self._pick_manual) and self._pick_manual[idx]
            painter.setPen(QPen(self._sel_color if manual else _COLOR_SELECTED, 2))
            painter.drawRect(self._local_rectf(phys))

        self._draw_pick_badges(painter)
        self._draw_preview(painter)

    def _draw_preview(self, painter: QPainter) -> None:
        """
        画鼠标底下那块的预览框：黄色虚线 + 尺寸 + 「第几层／共几层」。

        层级那个角标很重要——不写的话你不知道还能不能继续滚，
        也不知道自己滚到哪儿了。
        """
        preview = self.current_preview()
        if preview is None:
            return
        rect = self._local_rectf(preview)

        pen = QPen(_COLOR_PREVIEW, 2)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)

        text = f"{preview.width()} x {preview.height()}"
        if len(self._levels) > 1:
            text += f"   第 {self._level + 1}/{len(self._levels)} 层（滚轮切换）"

        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        tw = metrics.horizontalAdvance(text) + 12
        th = metrics.height() + 6

        # 默认贴在框的上沿外侧；顶到屏幕边了就翻到框内
        x = rect.left()
        y = rect.top() - th - 4
        if y < 0:
            y = rect.top() + 4
        x = max(0.0, min(x, self.width() - tw))

        box = QRectF(x, y, tw, th)
        painter.fillRect(box, QColor(0, 0, 0, 200))
        painter.setPen(QPen(_COLOR_PREVIEW))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_pick_badges(self, painter: QPainter) -> None:
        """
        在每个选中的框左上角画一个序号。

        序号就是保存的先后顺序，这样你在文件夹里看到的 _1 _2 和屏幕上
        看到的编号能对上。
        """
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        for n, phys in enumerate(self._picks, start=1):
            rect = self._local_rectf(phys)
            box = QRectF(rect.left(), rect.top(), 26, 20)
            painter.fillRect(box, _COLOR_SELECTED)
            painter.setPen(QPen(QColor(0, 0, 0)))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, str(n))

    def _draw_size_label(self, painter: QPainter, sel: QRectF) -> None:
        """在鼠标旁边显示选区的**物理**像素尺寸（也就是最终存出来的图的尺寸）。"""
        pw = max(1, round(sel.width() * self._smap.sx))
        ph = max(1, round(sel.height() * self._smap.sy))
        text = f"{pw} x {ph}"

        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        tw = metrics.horizontalAdvance(text) + 12
        th = metrics.height() + 6

        # 默认放在选区右下角外侧；贴边时翻到内侧，保证始终看得见
        x = sel.right() + 8
        y = sel.bottom() + 8
        if x + tw > self.width():
            x = sel.right() - tw - 8
        if y + th > self.height():
            y = sel.bottom() - th - 8
        x = max(0.0, x)
        y = max(0.0, y)

        box = QRectF(x, y, tw, th)
        painter.fillRect(box, QColor(0, 0, 0, 190))
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_hint(self, painter: QPainter) -> None:
        """
        还没开始框选时，在屏幕上方居中显示操作提示。

        自动模式下会多显示一行状态——「识别中…」「找到 8 个」「识别失败：……」。
        没有这一行的话，识别要跑一会儿的时候你会以为是按 Tab 没反应。
        """
        lines = [_HINT_AUTO if self._submode == SUBMODE_AUTO else _HINT_MANUAL]
        status = self._status_line()
        if status:
            lines.append(status)

        font = QFont()
        font.setPointSize(11)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        tw = max(metrics.horizontalAdvance(t) for t in lines) + 28
        th = metrics.height() * len(lines) + 16
        box = QRectF((self.width() - tw) / 2, self.height() * 0.08, tw, th)
        painter.fillRect(box, QColor(0, 0, 0, 170))
        painter.setPen(QPen(QColor(235, 235, 235)))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "\n".join(lines))

    def _status_line(self) -> str:
        """自动模式下那行状态文字。手动模式返回空串（不占地方）。"""
        if self._submode != SUBMODE_AUTO:
            return ""
        if self._detect_state == "running":
            return "正在分析画面…（这期间也可以直接拖框）"
        if self._detect_state == "failed":
            return f"分析失败：{self._detect_error}（还可以自己拖框）"
        if self._picks:
            return f"已选中 {len(self._picks)} 个，按 Enter 全部保存"
        if not self._levels:
            return "把鼠标移到想要的图片上"
        return "点击选中这一块；大小不对就滚一下滚轮"

    def _selection_rect(self) -> QRectF | None:
        """当前选区（窗口内逻辑坐标）。还没开始拖就返回 None。"""
        if self._origin is None or self._current is None:
            return None
        return QRectF(self._origin, self._current).normalized()

    # --- 鼠标 ---------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position()
            self._current = event.position()
            self._dragging = True
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            # 右键取消当前这一框（不退出遮罩，退出用 Esc）
            self._reset_selection()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._current = event.position()
            self.update()
        elif self._submode == SUBMODE_AUTO:
            self._probe(event.position())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            return
        self._dragging = False
        self._current = event.position()
        if self._origin is None:
            return

        moved = max(
            abs(self._current.x() - self._origin.x()),
            abs(self._current.y() - self._origin.y()),
        )

        # 自动模式下，「点一下」和「拖一段」是两件完全不同的事：
        #   点一下 = 选中鼠标底下那块（或者取消一个已经选中的）
        #   拖一段 = 自己补画一个框
        # 用移动距离区分。不这么做的话，想点选却手抖了 2 像素，
        # 就会莫名其妙多出一个 2x3 的小框。
        if self._submode == SUBMODE_AUTO and moved < _CLICK_SLOP_PX:
            self._click_at(self._current)
            self._reset_selection()
            return

        self._emit_region(
            self._origin.x(), self._origin.y(),
            self._current.x(), self._current.y(),
            trigger="拖框",
        )

    def _to_local_physical(self, pos: QPointF) -> tuple[int, int]:
        """窗口内逻辑坐标 -> 本屏物理坐标。识别是在物理像素上做的。"""
        return (round(pos.x() * self._smap.sx), round(pos.y() * self._smap.sy))

    def _probe(self, pos: QPointF) -> None:
        """
        算一遍鼠标当前位置能选出哪些区域。

        鼠标移动时会一直调这个，所以有两道省算力的措施：
          1. 移动不到 _PROBE_STEP_PX 个物理像素就跳过（结果本来也不会变）
          2. 层级序号尽量保持——你滚到第 3 层之后手抖一下，
             不该被打回第 1 层

        索引还没算好（刚进自动模式那一瞬间）时什么都不做，
        提示条上会显示「正在分析画面…」。
        """
        if self._index is None:
            return

        px, py = self._to_local_physical(pos)
        if self._last_probe is not None:
            dx = abs(px - self._last_probe[0])
            dy = abs(py - self._last_probe[1])
            if dx < _PROBE_STEP_PX and dy < _PROBE_STEP_PX:
                return
        self._last_probe = (px, py)

        try:
            from screen import detector

            params = detector.DetectParams.from_config(self._cfg)
            levels = detector.candidates_at(self._index, px, py, params)
        except Exception as e:
            # 查询出错不能让遮罩卡死——记下来，继续让用户手动拖框
            log.exception("查询候选区域失败：%s", e)
            self._detect_state = "failed"
            self._detect_error = str(e)
            self._levels = []
            self.update()
            return

        self._levels = levels
        self._level = min(self._level, max(0, len(levels) - 1))
        self.update()

    def current_preview(self) -> QRect | None:
        """当前预览的那个框（本屏物理坐标）。没有候选时返回 None。"""
        if not self._levels:
            return None
        idx = min(max(0, self._level), len(self._levels) - 1)
        return self._levels[idx]

    def wheelEvent(self, event) -> None:
        """
        滚轮：在「小块 -> 整张图 -> 更大的容器」之间切换。

        这是整个自动模式的关键操作。识别把一张图切成了两半时，
        往上滚一格就是完整的那张——不用调参数、不用重来。
        """
        if self._submode != SUBMODE_AUTO or not self._levels:
            super().wheelEvent(event)
            return

        delta = event.angleDelta().y()
        if delta == 0:
            return
        step = 1 if delta > 0 else -1
        new_level = min(max(0, self._level + step), len(self._levels) - 1)
        if new_level != self._level:
            self._level = new_level
            preview = self.current_preview()
            log.debug(
                "滚轮切到第 %d/%d 层：%s",
                self._level + 1, len(self._levels), preview,
            )
            self.update()
        event.accept()

    def _click_at(self, pos: QPointF) -> None:
        """
        点一下：

        - 点在**已经选中**的框里 -> 取消那个框（想删掉某一个时最顺手的做法）
        - 其它情况               -> 把当前预览的那块加进选中列表
        """
        px, py = self._to_local_physical(pos)

        # 已选中的框里，找包住鼠标且面积最小的那个
        best = -1
        best_area = None
        for idx, phys in enumerate(self._picks):
            if phys.contains(px, py):
                area = phys.width() * phys.height()
                if best_area is None or area < best_area:
                    best, best_area = idx, area
        if best >= 0:
            removed = self._picks.pop(best)
            if best < len(self._pick_manual):
                self._pick_manual.pop(best)
            log.info("取消选中：%s，还剩 %d 个", removed, len(self._picks))
            self._last_probe = None      # 强制下次移动重新查询
            self.update()
            return

        preview = self.current_preview()
        if preview is None:
            return
        self._picks.append(preview)
        self._pick_manual.append(False)
        log.info(
            "选中第 %d 个：本屏物理 %s（第 %d/%d 层）",
            len(self._picks), preview, self._level + 1, len(self._levels),
        )
        self.update()

    # --- 键盘 ---------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self.handle_key(event.key(), event.modifiers()):
            super().keyPressEvent(event)

    def handle_key(self, key: int, modifiers) -> bool:
        """
        统一的按键处理。

        参数：
            key       —— Qt.Key 枚举值
            modifiers —— Qt.KeyboardModifier 组合

        返回：这个键有没有被处理掉（True 表示已处理，调用方不要再传下去）。

        为什么单独抽一个方法：窗口自己的 keyPressEvent 和控制器的应用级事件
        过滤器都要处理按键。共用这一个方法，才不会出现「焦点在别处时 Enter
        没反应、焦点在窗口上时又有反应」这种极难排查的不一致。

        A 键在两个模式下都是「立刻整屏捕获」，它是坐标验证工具，
        任何时候都该是同一个行为。
        """
        if key == Qt.Key.Key_Escape:
            log.info("用户按 Esc 退出遮罩")
            self.cancelled.emit()
            return True
        if key == Qt.Key.Key_Tab:
            self.submode_toggle_requested.emit()
            return True
        if key == Qt.Key.Key_A:
            self.select_whole_screen()
            return True
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            return self.undo_pick()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit_requested.emit()
            return True
        return False

    def undo_pick(self) -> bool:
        """
        Backspace：撤销最后一次选中。

        返回：有没有真的撤销掉（没得撤时返回 False，按键继续往下传）。

        比「点回去取消」好用的地方：选错了直接按一下就行，
        不用再瞄准那个框点一次。
        """
        if self._submode != SUBMODE_AUTO or not self._picks:
            return False
        removed = self._picks.pop()
        if self._pick_manual:
            self._pick_manual.pop()
        log.info("撤销选中：%s，还剩 %d 个", removed, len(self._picks))
        self.update()
        return True

    # --- 自动模式的对外接口（给 OverlayController 调）-----------------------

    @property
    def submode(self) -> str:
        return self._submode

    @property
    def detect_state(self) -> str:
        return self._detect_state

    def set_submode(self, submode: str) -> None:
        """
        切换子模式。切到自动模式且索引还没算过时，顺便发出分析请求。

        已经算过就不再重复跑——你可能只是想切回手动补一框再切回来，
        没必要每次都等一遍，已经选中的东西也不该被清掉。
        """
        if submode == self._submode:
            return
        self._submode = submode
        self._reset_selection()
        self._levels = []
        self._level = 0
        self._last_probe = None
        log.info("屏幕 %s 切换到%s模式", self._smap.name, "自动" if submode == SUBMODE_AUTO else "手动")
        if submode == SUBMODE_AUTO and self._detect_state == "idle":
            self._detect_state = "running"
            self.detect_requested.emit()
        self.update()

    def set_index(self, index) -> None:
        """
        装入预计算好的候选索引（detector.RegionIndex）。

        装好之后鼠标一动就能出预览框。装之前鼠标动也不会有反应，
        提示条上显示的是「正在分析画面…」。
        """
        self._index = index
        self._detect_state = "done"
        self._detect_error = ""
        self._last_probe = None
        # 鼠标现在停在哪就先算哪一块，省得非要动一下才出框
        self._probe(self.mapFromGlobal(QCursor.pos()))
        self.update()

    def set_detect_failed(self, message: str) -> None:
        """分析失败。界面上要说清楚，并且提醒还能手动框——绝不静默。"""
        self._detect_state = "failed"
        self._detect_error = message
        self.update()

    def selected_regions(self) -> list[QRect]:
        """
        已选中的框，换算成**虚拟桌面物理坐标**，按你点选的先后顺序排好。

        存的是本屏物理坐标，这里只加上本屏原点，没有任何缩放换算，
        所以不存在精度损失。
        """
        return [
            QRect(self._smap.px + r.x(), self._smap.py + r.y(), r.width(), r.height())
            for r in self._picks
        ]

    def add_manual_box(self, virtual_rect: QRect) -> None:
        """
        把手动拖出来的框加进选中列表。

        参数 virtual_rect 是虚拟桌面物理坐标，这里转回本屏坐标存起来。

        直接就算选中：你特意拖了一个框，意图很明确就是要它。
        """
        local = QRect(
            virtual_rect.x() - self._smap.px,
            virtual_rect.y() - self._smap.py,
            virtual_rect.width(),
            virtual_rect.height(),
        )
        self._picks.append(local)
        self._pick_manual.append(True)
        log.info("手动补框：本屏物理 %s，当前共选中 %d 个", local, len(self._picks))
        self.update()

    def select_whole_screen(self) -> None:
        """
        选中整块屏幕。

        这个功能存在的最大意义是**验证坐标换算**：它完全不经过鼠标，
        所以存出来的图必须精确等于该屏的物理分辨率（你的机器是 2560x1600）。
        对不上就说明换算系数有问题。

        不管当前是手动还是自动模式，A 键都是**立刻保存**——它是个验证工具，
        要是在自动模式下变成「加一个整屏候选还得再按 Enter」，验证起来就绕了。
        """
        log.info("整屏捕获：%s", self._smap.describe())
        self._emit_region(
            0.0, 0.0, float(self.width()), float(self.height()),
            trigger="整屏(A键)", immediate=True,
        )

    # --- 坐标换算与提交 -----------------------------------------------------

    def _emit_region(
        self, lx0: float, ly0: float, lx1: float, ly1: float,
        trigger: str, immediate: bool = False,
    ) -> None:
        """
        把窗口内的逻辑坐标换算成虚拟桌面物理坐标，然后发出去。

        参数是拖拽的起点和终点（顺序无所谓，内部会排序）。

        这是整个项目最关键的一段计算，所以每一步都写进日志——
        以后你说「框选位置偏了」，我直接看这几行就能定位。
        """
        raw = (lx0, ly0, lx1, ly1)
        lx0, lx1 = sorted((lx0, lx1))
        ly0, ly1 = sorted((ly0, ly1))

        # 边缘吸附：150% 缩放下 1 个逻辑像素 = 1.5 个物理像素，靠手是不可能
        # 精确点到最后一个像素的。离边缘足够近就直接吸附过去。
        snapped: list[str] = []
        if self._snap > 0:
            if lx0 <= self._snap:
                lx0 = 0.0
                snapped.append("左")
            if ly0 <= self._snap:
                ly0 = 0.0
                snapped.append("上")
            if lx1 >= self.width() - self._snap:
                lx1 = float(self.width())
                snapped.append("右")
            if ly1 >= self.height() - self._snap:
                ly1 = float(self.height())
                snapped.append("下")

        px0, py0 = self._smap.to_physical(lx0, ly0)
        px1, py1 = self._smap.to_physical(lx1, ly1)
        px0, py0 = self._smap.clamp_physical(px0, py0)
        px1, py1 = self._smap.clamp_physical(px1, py1)

        w = px1 - px0
        h = py1 - py0

        log.info(
            "[%s] %s 逻辑原始=%s -> 逻辑规整=(%.2f,%.2f)-(%.2f,%.2f) 吸附=%s "
            "sx=%.6f sy=%.6f -> 物理=(%d,%d) %dx%d",
            trigger, self._smap.name, tuple(round(v, 2) for v in raw),
            lx0, ly0, lx1, ly1, "+".join(snapped) or "无",
            self._smap.sx, self._smap.sy, px0, py0, w, h,
        )

        if w < self._min_px or h < self._min_px:
            log.info("选区太小（%dx%d，阈值 %d），当作误触忽略", w, h, self._min_px)
            self._reset_selection()
            return

        rect = QRect(px0, py0, w, h)

        # 自动模式下拖出来的框是「补充候选」，不立刻落盘——
        # 你可能还要再补两个，最后一起按 Enter。immediate=True 的 A 键除外。
        if self._submode == SUBMODE_AUTO and not immediate:
            self.add_manual_box(rect)
            self._reset_selection()
            return

        self.region_selected.emit(rect)

    def _reset_selection(self) -> None:
        self._origin = None
        self._current = None
        self._dragging = False
        self.update()


class _DetectSignals(QObject):
    """
    识别任务 -> 主线程 的信号桥。

    QRunnable 本身不是 QObject，发不了信号，所以要单独挂一个。
    参数里的 int 是屏幕下标，用来知道结果该给哪个窗口。
    """

    done = Signal(int, object)
    failed = Signal(int, str)


class _DetectTask(QRunnable):
    """
    在线程池里预计算一块屏的候选索引。

    为什么不在主线程直接跑：实测 2560x1600 一屏约 50~60 毫秒，
    足够让界面卡一下了，而且屏幕多的时候会累加。
    CLAUDE.md 第 9 节也明确要求「主线程绝不阻塞」。

    线程安全：QImage 是隐式共享的值类型，只读访问跨线程是安全的。
    这里从头到尾只读，不碰任何 QWidget。
    """

    def __init__(self, index: int, image: QImage, params, signals: _DetectSignals):
        super().__init__()
        self._index = index
        self._image = image
        self._params = params
        self._signals = signals

    def run(self) -> None:
        try:
            from screen import detector

            region_index = detector.build_region_index(self._image, self._params)
            self._signals.done.emit(self._index, region_index)
        except ImportError as e:
            # 没装 opencv。这是最可能出现的失败，提示要说人话。
            log.error("自动识别不可用：%s", e)
            self._signals.failed.emit(self._index, "没装 opencv-python")
        except Exception as e:
            log.exception("预计算候选索引出错")
            self._signals.failed.emit(self._index, str(e))


class OverlayController(QObject):
    """
    遮罩的总调度：抓图 -> 建窗口 -> 收结果 -> 关窗口。

    跨窗口的事情都归它管（Tab 切模式、Enter 批量保存、调度识别），
    因为这些动作要同时影响所有屏，单个窗口没资格自己决定。

    发出的信号：
        captured(QImage, str)       —— 裁好的一张图 + 来源屏幕名
        captured_batch(list, str)   —— 批量：[QImage, ...] + 来源说明
        finished()                  —— 遮罩已经全部关掉（不管是成功还是取消）。
                                       main.py 收到它之后才重新启用全局热键。
    """

    captured = Signal(object, str)
    captured_batch = Signal(object, str)
    finished = Signal()

    def __init__(self, cfg, parent: QObject | None = None):
        super().__init__(parent)
        self._cfg = cfg
        self._windows: list[OverlayWindow] = []
        self._frozen: QImage | None = None
        self._virt: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._closing = False

        # 所有屏共用一个子模式。两块屏一块自动一块手动会让人彻底搞不清状态。
        self._submode = SUBMODE_MANUAL
        self._detect_signals = _DetectSignals()
        self._detect_signals.done.connect(self._on_detect_done)
        self._detect_signals.failed.connect(self._on_detect_failed)
        self._pool = QThreadPool.globalInstance()

    @property
    def active(self) -> bool:
        """遮罩是不是正显示着。热键重复触发时靠它判断。"""
        return bool(self._windows)

    def start(self) -> None:
        """
        抓图并弹出遮罩。

        什么情况会失败：截图失败（锁屏、远程桌面断开）时抛异常，
        由调用方捕获并提示用户。这里不吞异常。
        """
        if self._windows:
            log.debug("遮罩已经开着，忽略这次触发")
            return

        self._closing = False
        self._frozen, self._virt = capture.grab_virtual_desktop()

        # 每次唤起都从配置里的默认子模式重新开始，不沿用上一次的状态——
        # 按下热键时你脑子里想的是「我要截图」，不该还要先回忆上次停在哪个模式。
        requested = str(self._cfg.get("screen.default_submode", SUBMODE_MANUAL) or SUBMODE_MANUAL)
        self._submode = requested if requested in (SUBMODE_MANUAL, SUBMODE_AUTO) else SUBMODE_MANUAL
        if requested != self._submode:
            log.warning("screen.default_submode 的值 %r 无法识别，按手动模式处理", requested)

        screens = QGuiApplication.screens()
        maps = geometry.map_screens(screens)
        log.info("本次框选共 %d 块屏，虚拟桌面 %s：", len(maps), self._virt)
        for sm in maps:
            log.info("  %s", sm.describe())

        for sm in maps:
            pixmap = self._pixmap_for_screen(sm)
            win = OverlayWindow(sm, pixmap, self._cfg)
            win.region_selected.connect(self._on_region_selected)
            win.cancelled.connect(self._on_cancelled)
            win.submode_toggle_requested.connect(self.toggle_submode)
            win.commit_requested.connect(self.commit_selection)
            win.detect_requested.connect(self._on_detect_requested)
            self._windows.append(win)

        for win in self._windows:
            win.show()
            win.raise_()
            # 窗口显示后问一次 Windows「我到底在哪块屏上」。这是权威答案，
            # 和初始配对不一致时以它为准——这样即使 map_screens() 的配对
            # 猜错了（例如两台同型号显示器），坐标也依然是对的。
            actual = win.verify_monitor()
            if actual is not None and (
                actual.left, actual.top, actual.width, actual.height
            ) != (win.smap.px, win.smap.py, win.smap.pw, win.smap.ph):
                corrected = replace(
                    win.smap,
                    px=actual.left, py=actual.top,
                    pw=actual.width, ph=actual.height,
                    match_method="hwnd-corrected",
                )
                win.apply_screen_map(corrected, self._pixmap_for_screen(corrected))

        # 键盘焦点给鼠标当前所在的那块屏，这样 Esc / A 立刻可用
        focus_win = self._window_under_cursor() or self._windows[0]
        focus_win.activateWindow()
        focus_win.setFocus(Qt.FocusReason.OtherFocusReason)

        # 兜底：某些情况下无边框置顶窗口拿不到键盘焦点，
        # 装一个应用级事件过滤器，保证 Esc 和 A 一定有效。
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        # 配置里默认就是自动模式时，在这里统一切过去（会顺带触发识别）。
        # 必须放在 verify_monitor 之后：万一屏幕配对被纠正过，识别要用纠正后的画面。
        if self._submode == SUBMODE_AUTO:
            for win in self._windows:
                win.set_submode(SUBMODE_AUTO)

    # --- 子模式与自动识别（阶段 3）------------------------------------------

    def toggle_submode(self) -> None:
        """Tab：所有屏一起切换子模式。"""
        if not self._windows:
            return
        self._submode = SUBMODE_AUTO if self._submode == SUBMODE_MANUAL else SUBMODE_MANUAL
        for win in self._windows:
            win.set_submode(self._submode)

    def _on_detect_requested(self) -> None:
        """
        某个窗口请求识别自己那块屏。

        窗口没法自己跑识别——它拿不到冻结的原图（原图在控制器手里），
        也不该自己管线程池。
        """
        win = self.sender()
        if not isinstance(win, OverlayWindow) or win not in self._windows:
            return
        index = self._windows.index(win)

        try:
            from screen.detector import DetectParams

            params = DetectParams.from_config(self._cfg)
        except ImportError as e:
            log.error("载入识别参数失败：%s", e)
            win.set_detect_failed("没装 opencv-python")
            return

        image = self._image_for_screen(win.smap)
        log.info("开始分析屏幕 %s（%dx%d）", win.smap.name, image.width(), image.height())
        self._pool.start(_DetectTask(index, image, params, self._detect_signals))

    def _on_detect_done(self, index: int, region_index: object) -> None:
        """
        索引算好了（已经切回主线程）。

        遮罩可能在分析期间就被 Esc 关掉了，所以先检查窗口还在不在——
        不检查的话这里会对着已经 deleteLater 的窗口调方法，直接崩。
        """
        if self._closing or not (0 <= index < len(self._windows)):
            return
        self._windows[index].set_index(region_index)

    def _on_detect_failed(self, index: int, message: str) -> None:
        """分析失败。写日志 + 在遮罩上显示原因，不静默吞掉。"""
        if self._closing or not (0 <= index < len(self._windows)):
            return
        self._windows[index].set_detect_failed(message)

    def commit_selection(self) -> None:
        """
        Enter：把**所有屏**上选中的框一起裁出来保存。

        跨屏收集是必要的：你在副屏勾了两张、又切到主屏勾了三张，
        按一次 Enter 就该五张全存下来。
        """
        if self._closing or self._frozen is None:
            return

        rects: list[QRect] = []
        names: list[str] = []
        for win in self._windows:
            picked = win.selected_regions()
            if picked:
                rects.extend(picked)
                names.append(win.smap.name)

        if not rects:
            log.info("按了 Enter，但一个都没选中，忽略")
            return

        images: list[QImage] = []
        for rect in rects:
            cropped = self._crop(rect)
            if cropped is not None:
                images.append(cropped)

        if not images:
            log.error("选中了 %d 个框，但一张都没裁出来", len(rects))
            self._close_all()
            self.finished.emit()
            return

        source = "+".join(names) if names else "未知屏幕"
        log.info("批量保存 %d 张（来自 %s）", len(images), source)
        # 顺序和单张时一致：先关窗口，再发数据，最后发 finished
        self._close_all()
        self.captured_batch.emit(images, source)
        self.finished.emit()

    # --- 内部 ---------------------------------------------------------------

    def _crop(self, rect: QRect) -> QImage | None:
        """
        按虚拟桌面物理坐标从冻结画面上裁一块出来。

        返回 None 表示这个矩形和画面没有交集（正常情况下不该发生，
        真发生了会写 error 日志）。
        """
        if self._frozen is None:
            return None
        vx, vy = self._virt[0], self._virt[1]
        img_rect = QRect(rect.x() - vx, rect.y() - vy, rect.width(), rect.height())
        img_rect = img_rect.intersected(self._frozen.rect())
        if img_rect.width() < 1 or img_rect.height() < 1:
            log.error("裁剪区域与冻结画面没有交集：选区=%s 画面=%s", rect, self._frozen.rect())
            return None
        return self._frozen.copy(img_rect)

    def _image_for_screen(self, smap: ScreenMap) -> QImage:
        """从冻结的整张虚拟桌面图上，裁出属于这块屏的部分（QImage，给识别用）。"""
        assert self._frozen is not None
        vx, vy = self._virt[0], self._virt[1]
        return self._frozen.copy(QRect(smap.px - vx, smap.py - vy, smap.pw, smap.ph))

    def _pixmap_for_screen(self, smap: ScreenMap) -> QPixmap:
        """
        从冻结的整张虚拟桌面图上，裁出属于这块屏的部分。

        物理坐标要先减去虚拟桌面原点才是图内坐标——副屏摆在主屏左边时，
        物理坐标是负数，忘了这一步就会裁到图外面去（这一步在 _image_for_screen 里）。
        """
        sub = self._image_for_screen(smap)
        # devicePixelRatio 保持 1.0（默认值）：绘制时 OverlayWindow._draw_frozen()
        # 会把画笔切到设备像素坐标系，那里 1 个 pixmap 像素就是 1 个物理像素。
        # 千万别在这里设 DPR，设了反而会引入一次重采样。
        return QPixmap.fromImage(sub)

    def _window_under_cursor(self) -> OverlayWindow | None:
        """找出鼠标当前所在的那个遮罩窗口。"""
        pos = QCursor.pos()
        for win in self._windows:
            if win.geometry().contains(pos):
                return win
        return None

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """
        应用级键盘兜底。只在遮罩显示期间生效。

        为什么需要：Windows 上无边框 + 置顶的窗口偶尔拿不到键盘焦点，
        这时候 Esc 会失效，用户就被一张不动的假画面卡住了——必须留后路。
        """
        if not self._windows or event.type() != QEvent.Type.KeyPress:
            return False
        # 交给鼠标所在那块屏的窗口去处理，用的是和 keyPressEvent 完全相同的
        # 那个 handle_key()，所以不管键盘焦点在哪，行为都一模一样。
        win = self._window_under_cursor() or self._windows[0]
        return win.handle_key(event.key(), event.modifiers())

    def _on_region_selected(self, rect: QRect) -> None:
        """收到选区（虚拟桌面物理坐标），从冻结画面上裁出来。"""
        if self._closing or self._frozen is None:
            return
        source = "未知屏幕"
        sender = self.sender()
        if isinstance(sender, OverlayWindow):
            source = sender._smap.name

        cropped = self._crop(rect)
        if cropped is None:
            # 裁不出东西也必须发 finished，否则 main.py 不会把全局热键恢复回去，
            # 表现就是「出错一次之后热键彻底失灵」。
            self._close_all()
            self.finished.emit()
            return

        log.info("裁剪完成：%dx%d（来自 %s）", cropped.width(), cropped.height(), source)
        # 顺序有讲究：先关窗口（否则提示条会被遮罩盖住），
        # 再发 captured 让 main.py 落盘，最后才发 finished 恢复热键。
        self._close_all()
        self.captured.emit(cropped, source)
        self.finished.emit()

    def _on_cancelled(self) -> None:
        self._close_all()
        self.finished.emit()

    def _close_all(self) -> None:
        """关掉所有遮罩窗口、卸掉事件过滤器、释放冻结画面。可以重复调用。"""
        if self._closing:
            return
        self._closing = True

        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

        for win in self._windows:
            # 全部断开。分析任务可能还在线程池里跑着，断开之后它回来时
            # 找不到窗口就会自己退出（_on_detect_done 里有 _closing 判断兜底）。
            #
            # 对着一个没连接过的信号调 disconnect()，PySide6 不抛异常，
            # 而是往 stderr 打一条 RuntimeWarning。这里连一个都没漏地全断，
            # 所以要把那种警告压掉——控制台上多出几行红字，
            # 会让人以为程序出问题了。
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                for sig in (
                    win.region_selected, win.cancelled, win.detect_requested,
                    win.submode_toggle_requested, win.commit_requested,
                ):
                    try:
                        sig.disconnect()
                    except (RuntimeError, TypeError):
                        pass
            win.close()
            win.deleteLater()
        self._windows.clear()
        self._frozen = None
