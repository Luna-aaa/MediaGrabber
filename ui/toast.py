"""
右下角浮动提示条。

要求（CLAUDE.md 4.2）：显示文件名和保存路径，2 秒后淡出。

两个必须做对的细节：
  1. WA_ShowWithoutActivating —— 提示条弹出来时绝对不能抢走键盘焦点，
     否则你正在别的窗口里打字就会被打断。
  2. 多条提示要往上堆叠，不能重叠在一起。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QLabel,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

# 提示条的三种语气
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

_ACCENT = {
    LEVEL_INFO: "#22C55E",    # 绿：成功
    LEVEL_WARN: "#F59E0B",    # 橙：跳过 / 需要注意
    LEVEL_ERROR: "#EF4444",   # 红：失败
}

_MARGIN = 16        # 离屏幕边缘的距离（逻辑像素）
_GAP = 8            # 多条提示之间的间距
_FADE_MS = 400      # 淡出动画时长
_MAX_VISIBLE = 4    # 最多同时显示几条，超出的把最老的挤掉


class Toast(QWidget):
    """
    一条提示。自己管自己的生命周期：显示 -> 停留 -> 淡出 -> 销毁。

    不要直接 new 它，用 ToastManager.show() —— 那样才会正确排版堆叠。
    """

    def __init__(self, title: str, detail: str = "", level: str = LEVEL_INFO, duration_ms: int = 2000):
        super().__init__(None)
        self._duration = max(500, int(duration_ms))

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        # 关键：不抢焦点
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        accent = _ACCENT.get(level, _ACCENT[LEVEL_INFO])

        card = QWidget(self)
        card.setObjectName("card")
        card.setStyleSheet(
            f"""
            QWidget#card {{
                background-color: #1F2937;
                border-radius: 10px;
                border-left: 4px solid {accent};
            }}
            QLabel#title {{ color: #F9FAFB; }}
            QLabel#detail {{ color: #9CA3AF; }}
            """
        )

        inner = QVBoxLayout(card)
        inner.setContentsMargins(14, 10, 16, 12)
        inner.setSpacing(3)

        title_label = QLabel(title, card)
        title_label.setObjectName("title")
        f = QFont()
        f.setPointSize(10)
        f.setBold(True)
        title_label.setFont(f)
        title_label.setWordWrap(True)
        inner.addWidget(title_label)

        if detail:
            detail_label = QLabel(detail, card)
            detail_label.setObjectName("detail")
            df = QFont()
            df.setPointSize(9)
            detail_label.setFont(df)
            detail_label.setWordWrap(True)
            # 路径可能很长，限制宽度让它自动换行而不是把窗口撑爆
            detail_label.setMaximumWidth(360)
            inner.addWidget(detail_label)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 140))
        card.setGraphicsEffect(shadow)

        self.setMaximumWidth(400)
        self.adjustSize()

        self._fade: QPropertyAnimation | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.fade_out)

    def show_toast(self) -> None:
        """显示并开始计时。"""
        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        self._timer.start(self._duration)

    def fade_out(self) -> None:
        """淡出然后关闭。重复调用是安全的。"""
        if self._fade is not None:
            return
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(_FADE_MS)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade.finished.connect(self.close)
        self._fade.start()

    def mousePressEvent(self, event) -> None:
        """点一下立刻收起，不用等 2 秒。"""
        self._timer.stop()
        self.fade_out()


class ToastManager:
    """
    管理提示条的堆叠与排版。

    整个程序共用一个实例（main.py 里创建）。所有方法都必须在主线程调用——
    这是 Qt 的硬性要求，子线程要弹提示必须先通过信号切回主线程。
    """

    def __init__(self, duration_ms: int = 2000):
        self.duration_ms = duration_ms
        self._toasts: list[Toast] = []

    def show(self, title: str, detail: str = "", level: str = LEVEL_INFO) -> None:
        """
        弹一条提示。

        参数：
            title  —— 主标题，一般是文件名或结果概述
            detail —— 副标题，一般是完整保存路径
            level  —— info / warn / error，决定左侧色条的颜色

        什么情况会失败：这里不抛异常。创建窗口失败只写日志，
        因为提示条本身失败绝不该把主流程带崩。
        """
        try:
            toast = Toast(title, detail, level, self.duration_ms)
        except Exception:
            log.exception("创建提示条失败（不影响主流程）：%s / %s", title, detail)
            return

        toast.destroyed.connect(lambda *_: self._forget(toast))
        self._toasts.append(toast)

        # 超出上限就把最老的一条立刻收掉
        while len(self._toasts) > _MAX_VISIBLE:
            oldest = self._toasts[0]
            oldest.fade_out()
            break

        toast.show_toast()
        self._relayout()
        log.debug("提示条：[%s] %s | %s", level, title, detail)

    def _forget(self, toast: Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        self._relayout()

    def _relayout(self) -> None:
        """
        把所有提示条从右下角往上排。

        放在鼠标当前所在的那块屏上——多屏时你在哪块屏操作，提示就出现在哪块屏，
        不会跑到另一块屏上让你找不着。
        """
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()   # 已经排除了任务栏

        y = area.bottom() - _MARGIN
        for toast in reversed(self._toasts):
            if not toast.isVisible():
                continue
            size = toast.size()
            x = area.right() - size.width() - _MARGIN
            y -= size.height()
            toast.move(x, max(area.top() + _MARGIN, y))
            y -= _GAP

    def clear(self) -> None:
        """立刻收掉所有提示条（退出程序时用）。"""
        for toast in list(self._toasts):
            toast.close()
        self._toasts.clear()
