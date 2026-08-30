"""
主窗口：连接状态、页面扫描、缩略图网格、批量下载。

界面从上到下：
    ┌──────────────────────────────────────────────┐
    │ ● 已连接    [启动浏览器] [扫描当前页面] [登录状态]│
    ├──────────────────────────────────────────────┤
    │ 首次使用提示条（黄色，点「知道了」后不再出现）      │
    ├──────────────────────────────────────────────┤
    │ [全选][反选]  最小尺寸 ——●——— 200px   共 N 项    │
    ├──────────────────────────────────────────────┤
    │  ┌────┐ ┌────┐ ┌────┐ ┌────┐                 │
    │  │缩略│ │缩略│ │缩略│ │缩略│   ← 网格，可勾选   │
    │  └────┘ └────┘ └────┘ └────┘                 │
    ├──────────────────────────────────────────────┤
    │ [====进度条====]  已选 12 项  [下载选中][取消]  │
    └──────────────────────────────────────────────┘

线程约束：本文件只在 Qt 主线程执行。所有耗时操作都交给 BrowserController，
它内部有自己的工作线程，结果通过信号回来。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from browser import controller as bc
from browser import scanner

log = logging.getLogger(__name__)

CARD_WIDTH = 176
CARD_THUMB = 150
CARD_SPACING = 10

# 指示灯的三种颜色（CLAUDE.md 4.1：灰=未启动 / 黄=连接中 / 绿=已连接）
_LIGHT = {
    bc.STATE_DISCONNECTED: ("#9CA3AF", "未连接"),
    bc.STATE_CONNECTING: ("#F59E0B", "连接中"),
    bc.STATE_CONNECTED: ("#22C55E", "已连接"),
}


class MediaCard(QFrame):
    """
    网格里的一张卡片：缩略图 + 勾选框 + 尺寸/格式说明。

    下载结果会改变边框颜色：绿=成功，橙=已跳过，红=失败（悬停看原因）。
    """

    def __init__(self, index: int, item: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.index = index
        self.item = item
        self._state = "normal"

        self.setFixedWidth(CARD_WIDTH)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        # 用 objectName 做样式选择器，比按类名选更稳
        self.setObjectName("mediaCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self.thumb = QLabel(self)
        self.thumb.setFixedHeight(CARD_THUMB)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet("background:#111827; color:#6B7280; border-radius:4px;")
        self.thumb.setText(self._placeholder_text())
        layout.addWidget(self.thumb)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.checkbox = QCheckBox(self)
        self.checkbox.setChecked(True)
        row.addWidget(self.checkbox)

        self.info = QLabel(scanner.describe_item(item), self)
        self.info.setStyleSheet("color:#9CA3AF; font-size:11px;")
        row.addWidget(self.info, 1)
        layout.addLayout(row)

        # 不可下载的条目直接禁用勾选框，避免用户白白点了再失败
        note = str(item.get("note") or "")
        if note in (scanner.NOTE_STREAM, scanner.NOTE_DRM):
            self.checkbox.setChecked(False)
            self.checkbox.setEnabled(False)
            reason = "分片流媒体，不支持下载" if note == scanner.NOTE_STREAM else "DRM 加密内容，不支持下载"
            self.info.setText(reason)
            self.info.setStyleSheet("color:#F59E0B; font-size:11px;")

        self.setToolTip(self._tooltip())
        self._apply_style()

    def _placeholder_text(self) -> str:
        kind = str(self.item.get("kind") or "")
        return {"video": "视频", "poster": "封面", "background": "背景图"}.get(kind, "加载中…")

    def _tooltip(self) -> str:
        parts = [str(self.item.get("url") or "")]
        alt = str(self.item.get("alt") or "").strip()
        title = str(self.item.get("title") or "").strip()
        if alt:
            parts.append(f"alt: {alt}")
        if title:
            parts.append(f"title: {title}")
        return "\n".join(parts)

    # --- 状态 ---------------------------------------------------------------

    def set_thumbnail(self, data: bytes) -> None:
        """贴上缩略图。数据解不出来就保持占位文字，不报错。"""
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.thumb.setText("无法预览")
            return
        scaled = pixmap.scaled(
            CARD_WIDTH - 16, CARD_THUMB,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.thumb.setPixmap(scaled)
        # 之前尺寸未知的，现在从缩略图拿到真实尺寸了，顺手补上
        if not self.item.get("width") or not self.item.get("height"):
            self.item["width"] = pixmap.width()
            self.item["height"] = pixmap.height()
            self.info.setText(scanner.describe_item(self.item))

    def set_result(self, ok: bool, message: str) -> None:
        """标记下载结果。失败时边框变红，鼠标悬停显示原因。"""
        if not ok:
            self._state = "failed"
        elif "已存在" in message:
            self._state = "skipped"
        else:
            self._state = "ok"
        self.setToolTip(f"{self._tooltip()}\n\n{message}")
        self._apply_style()

    def reset_state(self) -> None:
        self._state = "normal"
        self.setToolTip(self._tooltip())
        self._apply_style()

    def _apply_style(self) -> None:
        border = {
            "normal": "#374151",
            "ok": "#22C55E",
            "skipped": "#F59E0B",
            "failed": "#EF4444",
        }[self._state]
        width = 1 if self._state == "normal" else 2
        self.setStyleSheet(
            f"QFrame#mediaCard {{ border:{width}px solid {border}; border-radius:6px; }}"
        )

    @property
    def checked(self) -> bool:
        return self.checkbox.isChecked() and self.checkbox.isEnabled()


class LoginStateDialog(QDialog):
    """
    登录状态面板（CLAUDE.md 第 6.4 节）。

    列出受控浏览器里有 cookie 的域名，让你一眼看出哪些站点已经登录过；
    并提供「清除登录数据」按钮（需二次确认）。
    """

    clear_requested = Signal()

    def __init__(self, domains: list[str], profile_path, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("登录状态")
        self.resize(420, 460)

        layout = QVBoxLayout(self)

        tip = QLabel(
            "下面是受控浏览器里存有 cookie 的网站。\n"
            "有 cookie 通常意味着你在这个站点登录过（但不保证登录仍然有效）。",
            self,
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#9CA3AF;")
        layout.addWidget(tip)

        self.list = QListWidget(self)
        if domains:
            self.list.addItems(domains)
        else:
            self.list.addItem("（还没有任何 cookie —— 先连接浏览器并登录一个网站）")
        layout.addWidget(self.list, 1)

        path_label = QLabel(f"数据目录：{profile_path}", self)
        path_label.setWordWrap(True)
        path_label.setStyleSheet("color:#6B7280; font-size:11px;")
        layout.addWidget(path_label)

        clear_btn = QPushButton("清除登录数据…", self)
        clear_btn.setStyleSheet("color:#EF4444;")
        clear_btn.clicked.connect(self._confirm_clear)
        layout.addWidget(clear_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _confirm_clear(self) -> None:
        answer = QMessageBox.warning(
            self,
            "确认清除登录数据",
            "这会删除整个 chrome-profile 目录，你在受控浏览器里的\n"
            "所有登录状态、Cookie、书签都会消失，且无法恢复。\n\n"
            "清除前请先把受控 Chrome 完全关掉，否则会删不干净。\n\n"
            "确定要继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.clear_requested.emit()
            self.accept()


class MainWindow(QWidget):
    """浏览器模式的主界面。"""

    def __init__(self, cfg, controller: bc.BrowserController, parent: QWidget | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self.controller = controller

        self._items: list[dict[str, Any]] = []
        self._cards: list[MediaCard] = []
        self._download_cards: list[MediaCard] = []
        self._domains: list[str] = []
        # (列数, 可见卡片的序号元组)。和上次一样就不用重排。
        self._layout_signature: tuple | None = None

        self.setWindowTitle("MediaGrabber —— 浏览器模式")
        self.resize(940, 720)
        self._build_ui()
        self._connect_signals()
        self._update_counts()

    # --- 界面搭建 -----------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # 顶部：指示灯 + 操作按钮
        top = QHBoxLayout()
        self.light = QLabel("●", self)
        self.light.setStyleSheet("color:#9CA3AF; font-size:18px;")
        top.addWidget(self.light)
        self.state_label = QLabel("未连接", self)
        top.addWidget(self.state_label)
        top.addStretch(1)

        self.btn_connect = QPushButton("启动浏览器", self)
        self.btn_scan = QPushButton("扫描当前页面", self)
        self.btn_scan.setEnabled(False)
        self.btn_login = QPushButton("登录状态", self)
        for b in (self.btn_connect, self.btn_scan, self.btn_login):
            top.addWidget(b)
        root.addLayout(top)

        # 首次使用提示条
        self.hint_bar = QFrame(self)
        self.hint_bar.setStyleSheet(
            "background:#78350F; border:1px solid #F59E0B; border-radius:6px;"
        )
        hint_layout = QHBoxLayout(self.hint_bar)
        hint_layout.setContentsMargins(12, 8, 8, 8)
        hint_text = QLabel(
            "首次使用：请在弹出的浏览器中登录你需要抓取的网站。"
            "登录状态会自动保存，以后不用重复登录。",
            self.hint_bar,
        )
        hint_text.setWordWrap(True)
        hint_text.setStyleSheet("color:#FEF3C7; border:none;")
        hint_layout.addWidget(hint_text, 1)
        hint_ok = QPushButton("知道了", self.hint_bar)
        hint_ok.clicked.connect(self._dismiss_hint)
        hint_layout.addWidget(hint_ok)
        self.hint_bar.setVisible(False)
        root.addWidget(self.hint_bar)

        # 筛选栏
        filt = QHBoxLayout()
        self.btn_all = QPushButton("全选", self)
        self.btn_invert = QPushButton("反选", self)
        filt.addWidget(self.btn_all)
        filt.addWidget(self.btn_invert)
        filt.addSpacing(16)
        filt.addWidget(QLabel("最小尺寸", self))
        self.size_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.size_slider.setRange(0, 2000)
        self.size_slider.setSingleStep(50)
        self.size_slider.setPageStep(100)
        self.size_slider.setValue(0)
        self.size_slider.setFixedWidth(190)
        filt.addWidget(self.size_slider)
        self.size_label = QLabel("不限", self)
        self.size_label.setFixedWidth(64)
        filt.addWidget(self.size_label)
        filt.addStretch(1)
        self.count_label = QLabel("", self)
        self.count_label.setStyleSheet("color:#9CA3AF;")
        filt.addWidget(self.count_label)
        root.addLayout(filt)

        # 缩略图网格
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(CARD_SPACING)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(self.grid_host)
        root.addWidget(self.scroll, 1)

        self.empty_label = QLabel(
            "还没有扫描结果。\n\n"
            "1. 点「启动浏览器」，会打开一个专用的 Chrome 窗口\n"
            "2. 在那个窗口里打开你要抓的页面（需要登录的先登录）\n"
            "3. 回到这里点「扫描当前页面」",
            self,
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet("color:#6B7280;")
        root.addWidget(self.empty_label, 1)

        # 底部：进度 + 下载
        bottom = QHBoxLayout()
        self.progress = QProgressBar(self)
        self.progress.setVisible(False)
        bottom.addWidget(self.progress, 1)
        self.summary = QLabel("", self)
        bottom.addWidget(self.summary)
        self.btn_download = QPushButton("下载选中", self)
        self.btn_download.setEnabled(False)
        self.btn_cancel = QPushButton("取消", self)
        self.btn_cancel.setVisible(False)
        bottom.addWidget(self.btn_download)
        bottom.addWidget(self.btn_cancel)
        root.addLayout(bottom)

        # 窗口尺寸变化时重排网格，用定时器防抖
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(120)
        self._relayout_timer.timeout.connect(self._relayout_grid)

    def _connect_signals(self) -> None:
        self.btn_connect.clicked.connect(self.controller.connect_browser)
        self.btn_scan.clicked.connect(self._on_scan_clicked)
        self.btn_login.clicked.connect(self._show_login_dialog)
        self.btn_all.clicked.connect(lambda: self._set_all_checked(True))
        self.btn_invert.clicked.connect(self._invert_selection)
        self.btn_download.clicked.connect(self._on_download_clicked)
        self.btn_cancel.clicked.connect(self.controller.cancel_download)
        self.size_slider.valueChanged.connect(self._on_size_filter_changed)

        self.controller.state_changed.connect(self.on_state_changed)
        self.controller.scan_finished.connect(self.on_scan_finished)
        self.controller.scan_failed.connect(self.on_scan_failed)
        self.controller.thumbnail_ready.connect(self.on_thumbnail_ready)
        self.controller.download_progress.connect(self.on_download_progress)
        self.controller.download_item_result.connect(self.on_download_item_result)
        self.controller.download_finished.connect(self.on_download_finished)
        self.controller.login_domains.connect(self.on_login_domains)
        self.controller.first_run_needed.connect(self.show_first_run_hint)

    # --- 控制器信号 ---------------------------------------------------------

    def on_state_changed(self, state: str, message: str) -> None:
        color, text = _LIGHT.get(state, _LIGHT[bc.STATE_DISCONNECTED])
        self.light.setStyleSheet(f"color:{color}; font-size:18px;")
        self.state_label.setText(f"{text}　{message}" if message else text)
        connected = state == bc.STATE_CONNECTED
        self.btn_scan.setEnabled(connected)
        self.btn_connect.setText("重新连接" if connected else "启动浏览器")

    def _on_scan_clicked(self) -> None:
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText("扫描中…")
        self.summary.setText("")
        self.controller.scan_page()

    def on_scan_finished(self, items: list) -> None:
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("扫描当前页面")
        self._items = list(items)
        self._rebuild_cards()
        if self._items:
            self.controller.load_thumbnails(self._items)
            self.summary.setText(f"扫描到 {len(self._items)} 项")
        else:
            self.summary.setText("这个页面没扫到符合条件的媒体")

    def on_scan_failed(self, message: str) -> None:
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("扫描当前页面")
        QMessageBox.warning(self, "扫描失败", message)

    def on_thumbnail_ready(self, index: int, data: bytes) -> None:
        if 0 <= index < len(self._cards):
            self._cards[index].set_thumbnail(bytes(data))

    def on_download_progress(self, done: int, total: int) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def on_download_item_result(self, index: int, ok: bool, message: str) -> None:
        if 0 <= index < len(self._download_cards):
            self._download_cards[index].set_result(ok, message)

    def on_download_finished(self, ok: int, failed: int, skipped: int) -> None:
        self.progress.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_download.setEnabled(True)
        parts = [f"成功 {ok} 张"]
        if skipped:
            parts.append(f"已跳过 {skipped} 张")
        if failed:
            parts.append(f"失败 {failed} 张")
        text = "，".join(parts)
        if failed:
            text += "（失败的卡片是红框，鼠标悬停看原因）"
        self.summary.setText(text)

    def on_login_domains(self, domains: list) -> None:
        self._domains = [str(d) for d in domains]

    def show_first_run_hint(self) -> None:
        """首次使用（chrome-profile 原先不存在）时显示黄色提示条。"""
        if bool(self.cfg.get("browser.first_run_hint_shown", False)):
            return
        self.hint_bar.setVisible(True)

    def _dismiss_hint(self) -> None:
        self.hint_bar.setVisible(False)
        try:
            self.cfg.set("browser.first_run_hint_shown", True)
        except OSError as e:
            log.warning("保存「首次提示已关闭」状态失败：%s", e)

    # --- 网格 ---------------------------------------------------------------

    def _rebuild_cards(self) -> None:
        """按扫描结果重建整个网格。"""
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._download_cards.clear()

        for i, item in enumerate(self._items):
            card = MediaCard(i, item, self.grid_host)
            # 勾一下就要立刻更新「已选 N」，否则计数会骗人
            card.checkbox.toggled.connect(self._update_counts)
            self._cards.append(card)

        self.empty_label.setVisible(not self._items)
        self.scroll.setVisible(bool(self._items))
        self.btn_download.setEnabled(bool(self._items))
        self._layout_signature = None      # 强制重排
        self._apply_size_filter()

    def _relayout_grid(self) -> None:
        """
        按当前窗口宽度决定列数，重新摆放**可见**的卡片。

        只摆可见的：被尺寸筛选隐藏的卡片如果还占着格子，网格里会留下一堆空洞。
        列数和可见集合都没变时直接跳过，避免频繁重排卡顿。
        """
        width = max(1, self.scroll.viewport().width())
        columns = max(1, width // (CARD_WIDTH + CARD_SPACING))
        visible = [c for c in self._cards if not c.isHidden()]
        signature = (columns, tuple(c.index for c in visible))
        if signature == self._layout_signature:
            return
        self._layout_signature = signature

        while self.grid.count():
            self.grid.takeAt(0)
        for i, card in enumerate(visible):
            self.grid.addWidget(card, i // columns, i % columns)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout_timer.start()

    def _set_all_checked(self, value: bool) -> None:
        for card in self._cards:
            if not card.isHidden() and card.checkbox.isEnabled():
                card.checkbox.setChecked(value)
        self._update_counts()

    def _invert_selection(self) -> None:
        for card in self._cards:
            if not card.isHidden() and card.checkbox.isEnabled():
                card.checkbox.setChecked(not card.checkbox.isChecked())
        self._update_counts()

    def _on_size_filter_changed(self, value: int) -> None:
        self.size_label.setText("不限" if value == 0 else f"{value}px")
        self._apply_size_filter()

    def _apply_size_filter(self) -> None:
        """
        按最小尺寸隐藏卡片。

        尺寸未知的条目（懒加载图在扫描时还没加载）**一律保留**，
        否则会把最想要的那些图筛没了。
        """
        threshold = self.size_slider.value()
        for card in self._cards:
            w = int(card.item.get("width") or 0)
            h = int(card.item.get("height") or 0)
            known = w > 0 and h > 0
            card.setHidden(known and (w < threshold or h < threshold))
        self._relayout_grid()
        self._update_counts()

    def _update_counts(self) -> None:
        # 用 isHidden() 而不是 isVisible()：窗口自己还没显示时，
        # 所有子控件的 isVisible() 都是 False，会把计数算成 0。
        visible = [c for c in self._cards if not c.isHidden()]
        selected = [c for c in visible if c.checked]
        self.count_label.setText(f"显示 {len(visible)} / 共 {len(self._cards)} 项，已选 {len(selected)}")

    # --- 下载 ---------------------------------------------------------------

    def _on_download_clicked(self) -> None:
        self._update_counts()
        chosen = [c for c in self._cards if not c.isHidden() and c.checked]
        if not chosen:
            QMessageBox.information(self, "没有选中任何项", "请先勾选要下载的图片。")
            return

        for card in chosen:
            card.reset_state()
        self._download_cards = chosen

        self.progress.setVisible(True)
        self.progress.setMaximum(len(chosen))
        self.progress.setValue(0)
        self.btn_cancel.setVisible(True)
        self.btn_download.setEnabled(False)
        self.summary.setText(f"正在下载 {len(chosen)} 项…")
        self.controller.download_items([c.item for c in chosen])

    # --- 登录状态 -----------------------------------------------------------

    def _show_login_dialog(self) -> None:
        self.controller.refresh_login_domains()
        dialog = LoginStateDialog(self._domains, self.cfg.profile_path(), self)
        dialog.clear_requested.connect(self._clear_login_data)
        dialog.exec()

    def _clear_login_data(self) -> None:
        ok, message = bc.clear_profile(self.cfg.profile_path())
        if ok:
            QMessageBox.information(self, "已清除", message)
            self._domains = []
        else:
            QMessageBox.critical(self, "清除失败", message)
