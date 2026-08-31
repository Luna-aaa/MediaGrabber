"""
历史记录面板（阶段 3）。

显示 history.jsonl 里的保存记录：时间、来源、路径、缩略图。
双击任意一条就在资源管理器里定位到那个文件（CLAUDE.md 4.3）。

两个刻意的设计：

1. **缩略图分批加载。**
   一次读几百个文件会把界面卡死好几秒。这里每 30 毫秒读 12 条，
   界面全程可以滚动、可以点击。列表刷新时旧的加载任务会立刻作废。

2. **文件被删掉的记录仍然显示，但标成灰色。**
   直接隐藏的话你会以为记录丢了。灰着显示能让你明白「存过，但文件已经不在了」。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

THUMB_SIZE = 96
# 一次加载几条缩略图。太大界面会卡，太小加载得慢。
_BATCH = 12
_BATCH_INTERVAL_MS = 30
# 最多显示多少条。记录攒到几千条时全读出来没有意义，也拖慢界面。
_MAX_ROWS = 500


def reveal_in_explorer(path: Path) -> bool:
    """
    在资源管理器里选中某个文件。

    参数：path —— 要定位的文件

    返回：有没有成功发起。文件不存在时返回 False（调用方负责提示）。

    注意 explorer 的参数格式很特别：/select 和路径之间是**逗号**不是空格，
    而且路径必须用引号包起来，否则带空格的路径会被截断。
    另外 explorer 正常情况下也会返回非 0 退出码，所以不能拿返回码判断成败。
    """
    if not path.exists():
        return False
    try:
        subprocess.Popen(f'explorer /select,"{path}"', shell=True)
        return True
    except OSError as e:
        log.error("调用资源管理器失败：%s", e)
        return False


class HistoryPanel(QWidget):
    """
    历史记录列表。

    参数：
        history —— core.history.History 实例，和落盘用的是同一个对象，
                   所以刚存完的图刷新一下就能看到
    """

    def __init__(self, history, parent: QWidget | None = None):
        super().__init__(parent)
        self._history = history
        self._rows: list[dict[str, Any]] = []
        self._pending: list[int] = []   # 还没加载缩略图的行号

        self._thumb_timer = QTimer(self)
        self._thumb_timer.setInterval(_BATCH_INTERVAL_MS)
        self._thumb_timer.timeout.connect(self._load_next_batch)

        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新", self)
        self.btn_refresh.clicked.connect(self.refresh)
        top.addWidget(self.btn_refresh)
        top.addStretch(1)
        self.count_label = QLabel("", self)
        self.count_label.setStyleSheet("color:#9CA3AF;")
        top.addWidget(self.count_label)
        root.addLayout(top)

        self.list = QListWidget(self)
        self.list.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_menu)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        root.addWidget(self.list, 1)

        self.tip = QLabel("双击任意一条，就能在资源管理器里定位到那个文件", self)
        self.tip.setStyleSheet("color:#6B7280; font-size:11px;")
        root.addWidget(self.tip)

    # --- 数据 ---------------------------------------------------------------

    def refresh(self) -> None:
        """
        重新读 history.jsonl 并重建列表。

        每次都从磁盘重读，而不是用内存里的缓存——浏览器模式的下载是在
        别的线程里写记录的，只有重读才能保证看到的是最新的。
        """
        self._thumb_timer.stop()
        self._pending.clear()
        self.list.clear()

        try:
            self._rows = self._history.entries(limit=_MAX_ROWS, newest_first=True)
        except Exception as e:
            log.exception("读取历史记录失败")
            self._rows = []
            self.count_label.setText(f"读取失败：{e}")
            return

        total = len(self._history)
        if not self._rows:
            self.count_label.setText("还没有任何记录")
            placeholder = QListWidgetItem("还没有保存过任何文件。\n按 Ctrl+Alt+S 截个图试试。")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(placeholder)
            return

        shown = len(self._rows)
        self.count_label.setText(
            f"共 {total} 条" if shown >= total else f"共 {total} 条，显示最近 {shown} 条"
        )

        for row_index, entry in enumerate(self._rows):
            item = QListWidgetItem(self._format_entry(entry))
            path = Path(str(entry.get("path") or ""))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            if not path.exists():
                # 文件已经被删了：标灰，但不隐藏
                item.setForeground(QColor("#6B7280"))
                item.setText(item.text() + "     （文件已不在）")
            else:
                self._pending.append(row_index)
            self.list.addItem(item)

        if self._pending:
            self._thumb_timer.start()

    def _format_entry(self, entry: dict[str, Any]) -> str:
        """把一条记录排成两行文字：概要 + 完整路径。"""
        when = str(entry.get("time") or "").replace("T", " ")
        kind = {"screen": "屏幕", "browser": "网页"}.get(str(entry.get("kind")), "其它")
        w, h = entry.get("width") or 0, entry.get("height") or 0
        size_text = f"{w}x{h}" if w and h else ""
        size_kb = int(entry.get("size") or 0) / 1024
        volume = f"{size_kb / 1024:.1f} MB" if size_kb >= 1024 else f"{size_kb:.0f} KB"

        head = "  ·  ".join(x for x in (when, kind, size_text, volume) if x)
        source = str(entry.get("source") or "")
        if source:
            head += f"  ·  来自 {source[:60]}"
        return f"{head}\n{entry.get('path') or ''}"

    def _load_next_batch(self) -> None:
        """
        加载下一批缩略图。定时器驱动，每次只做一点点，界面不会卡。

        读不出来的（文件损坏、格式不支持）就跳过不设图标，不报错——
        缩略图只是锦上添花，不该因为它挡住你看记录。
        """
        if not self._pending:
            self._thumb_timer.stop()
            return

        for _ in range(_BATCH):
            if not self._pending:
                self._thumb_timer.stop()
                return
            row = self._pending.pop(0)
            item = self.list.item(row)
            if item is None:
                continue
            path = Path(str(item.data(Qt.ItemDataRole.UserRole) or ""))
            try:
                pm = QPixmap(str(path))
                if pm.isNull():
                    continue
                item.setIcon(QIcon(pm.scaled(
                    THUMB_SIZE, THUMB_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )))
            except Exception:
                log.debug("生成缩略图失败：%s", path, exc_info=True)

    # --- 交互 ---------------------------------------------------------------

    def _selected_path(self) -> Path | None:
        item = self.list.currentItem()
        if item is None:
            return None
        raw = str(item.data(Qt.ItemDataRole.UserRole) or "")
        return Path(raw) if raw else None

    def _on_double_click(self, item: QListWidgetItem) -> None:
        raw = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if raw:
            self._reveal(Path(raw))

    def _reveal(self, path: Path) -> None:
        if not reveal_in_explorer(path):
            self.count_label.setText(f"文件已经不在了：{path}")

    def _show_menu(self, pos) -> None:
        """右键菜单：定位、打开、复制路径。"""
        path = self._selected_path()
        if path is None:
            return

        menu = QMenu(self)
        act_reveal = QAction("在资源管理器中显示", menu)
        act_reveal.triggered.connect(lambda: self._reveal(path))
        menu.addAction(act_reveal)

        act_open = QAction("打开这个文件", menu)
        act_open.triggered.connect(lambda: self._open(path))
        menu.addAction(act_open)

        menu.addSeparator()
        act_copy = QAction("复制路径", menu)
        act_copy.triggered.connect(lambda: QApplication.clipboard().setText(str(path)))
        menu.addAction(act_copy)

        menu.exec(self.list.mapToGlobal(pos))

    def _open(self, path: Path) -> None:
        """用系统默认程序打开。文件不在了就在计数栏里说明，不弹窗打断。"""
        import os

        if not path.exists():
            self.count_label.setText(f"文件已经不在了：{path}")
            return
        try:
            os.startfile(str(path))
        except OSError as e:
            log.error("打开文件失败：%s", e)
            self.count_label.setText(f"打不开：{e}")
