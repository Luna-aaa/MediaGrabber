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

阶段 1 只实现手动框选。自动识别（Tab 切换）在阶段 3 加进来。
"""

from __future__ import annotations

import logging
from dataclasses import replace

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, Signal
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

# 提示条文案。放在这里方便以后改。
_HINT_TEXT = "拖动鼠标框选  ·  A = 整屏  ·  右键取消当前框  ·  Esc 退出"


class OverlayWindow(QWidget):
    """
    盖住**一块**屏幕的遮罩窗口。

    发出的信号：
        region_selected(QRect) —— 用户框好了。QRect 是【虚拟桌面物理像素】坐标。
        cancelled()            —— 用户按了 Esc，整个遮罩都该关掉。
    """

    region_selected = Signal(object)
    cancelled = Signal()

    def __init__(self, smap: ScreenMap, pixmap: QPixmap, cfg, parent=None):
        super().__init__(parent)
        self._smap = smap
        self._pixmap = pixmap
        self._cfg = cfg

        self._origin: QPointF | None = None    # 按下时的窗口内逻辑坐标
        self._current: QPointF | None = None   # 当前鼠标的窗口内逻辑坐标
        self._dragging = False

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
        """还没开始框选时，在屏幕上方居中显示操作提示。"""
        font = QFont()
        font.setPointSize(11)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        tw = metrics.horizontalAdvance(_HINT_TEXT) + 28
        th = metrics.height() + 16
        box = QRectF((self.width() - tw) / 2, self.height() * 0.08, tw, th)
        painter.fillRect(box, QColor(0, 0, 0, 170))
        painter.setPen(QPen(QColor(235, 235, 235)))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, _HINT_TEXT)

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

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            return
        self._dragging = False
        self._current = event.position()
        if self._origin is None:
            return
        self._emit_region(
            self._origin.x(), self._origin.y(),
            self._current.x(), self._current.y(),
            trigger="拖框",
        )

    # --- 键盘 ---------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            log.info("用户按 Esc 退出遮罩")
            self.cancelled.emit()
        elif key == Qt.Key.Key_A:
            self.select_whole_screen()
        else:
            super().keyPressEvent(event)

    def select_whole_screen(self) -> None:
        """
        选中整块屏幕。

        这个功能存在的最大意义是**验证坐标换算**：它完全不经过鼠标，
        所以存出来的图必须精确等于该屏的物理分辨率（你的机器是 2560x1600）。
        对不上就说明换算系数有问题。
        """
        log.info("整屏捕获：%s", self._smap.describe())
        self._emit_region(0.0, 0.0, float(self.width()), float(self.height()), trigger="整屏(A键)")

    # --- 坐标换算与提交 -----------------------------------------------------

    def _emit_region(self, lx0: float, ly0: float, lx1: float, ly1: float, trigger: str) -> None:
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

        self.region_selected.emit(QRect(px0, py0, w, h))

    def _reset_selection(self) -> None:
        self._origin = None
        self._current = None
        self._dragging = False
        self.update()


class OverlayController(QObject):
    """
    遮罩的总调度：抓图 -> 建窗口 -> 收结果 -> 关窗口。

    发出的信号：
        captured(QImage, str) —— 裁好的图 + 来源屏幕名
        finished()            —— 遮罩已经全部关掉（不管是成功还是取消）。
                                 main.py 收到它之后才重新启用全局热键。
    """

    captured = Signal(object, str)
    finished = Signal()

    def __init__(self, cfg, parent: QObject | None = None):
        super().__init__(parent)
        self._cfg = cfg
        self._windows: list[OverlayWindow] = []
        self._frozen: QImage | None = None
        self._virt: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._closing = False

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

    # --- 内部 ---------------------------------------------------------------

    def _pixmap_for_screen(self, smap: ScreenMap) -> QPixmap:
        """
        从冻结的整张虚拟桌面图上，裁出属于这块屏的部分。

        物理坐标要先减去虚拟桌面原点才是图内坐标——副屏摆在主屏左边时，
        物理坐标是负数，忘了这一步就会裁到图外面去。
        """
        assert self._frozen is not None
        vx, vy = self._virt[0], self._virt[1]
        rect = QRect(smap.px - vx, smap.py - vy, smap.pw, smap.ph)
        sub = self._frozen.copy(rect)
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
        key = event.key()
        if key == Qt.Key.Key_Escape:
            log.info("用户按 Esc 退出遮罩（应用级兜底）")
            self._on_cancelled()
            return True
        if key == Qt.Key.Key_A:
            win = self._window_under_cursor() or self._windows[0]
            win.select_whole_screen()
            return True
        return False

    def _on_region_selected(self, rect: QRect) -> None:
        """收到选区（虚拟桌面物理坐标），从冻结画面上裁出来。"""
        if self._closing or self._frozen is None:
            return
        source = "未知屏幕"
        sender = self.sender()
        if isinstance(sender, OverlayWindow):
            source = sender._smap.name

        vx, vy = self._virt[0], self._virt[1]
        img_rect = QRect(rect.x() - vx, rect.y() - vy, rect.width(), rect.height())
        # 再夹一次，防止任何意外导致越界（QImage.copy 越界会填黑边）
        img_rect = img_rect.intersected(self._frozen.rect())
        if img_rect.width() < 1 or img_rect.height() < 1:
            log.error("裁剪区域与冻结画面没有交集：选区=%s 画面=%s", rect, self._frozen.rect())
            self._close_all()
            return

        cropped = self._frozen.copy(img_rect)
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
            try:
                win.region_selected.disconnect()
                win.cancelled.disconnect()
            except (RuntimeError, TypeError):
                pass
            win.close()
            win.deleteLater()
        self._windows.clear()
        self._frozen = None
