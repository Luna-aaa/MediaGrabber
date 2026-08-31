"""
设置面板（阶段 3）+ 登录状态面板（CLAUDE.md 第 6.4 节）。

设置面板按功能分了 5 个标签页，每一项底下都写了一句人话说明——
你不用去翻 config.json，也不用记住哪个数字是干什么的。

哪些改动立刻生效、哪些要重启：
    保存目录、识别阈值、遮罩透明度  ->  下次用就是新的，不用重启
    热键、日志级别                  ->  点保存的那一刻就重新装好了，也不用重启
    浏览器并发/间隔                  ->  下一次扫描或下载时生效
真正需要重启的项目一个都没有。有的话我会在界面上直接标出来。

为什么 LoginStateDialog 也放在这个文件里：
它本质上是「设置」的一部分（CLAUDE.md 把它归在设置面板里）。
放这儿还能顺手解决一个循环导入——main_window 要用它，
而设置面板不需要反过来认识 main_window。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)


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


def _hint(text: str) -> QLabel:
    """生成一行灰色小字说明。每个设置项底下都该有一句人话。"""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color:#6B7280; font-size:11px;")
    return label


class SettingsDialog(QDialog):
    """
    设置面板。

    参数：
        cfg              —— config.Config 实例（就是全局那个，改完直接落盘）
        log_dir          —— 日志目录，用于「打开日志目录」按钮
        login_provider   —— 一个可调用对象，返回 (域名列表, profile 路径)。
                            没有浏览器控制器时传 None，登录状态按钮会禁用。
        clear_login      —— 清除登录数据的函数，返回 (是否成功, 说明文字)

    发出的信号：
        applied(dict) —— 保存成功。参数是所有被改动的字段：
                         {"hotkey.capture": (旧值, 新值), ...}
                         main.py 靠它决定要不要重新注册热键、重设日志级别。
    """

    applied = Signal(dict)

    def __init__(
        self,
        cfg,
        log_dir: Path | None = None,
        login_provider: Callable[[], tuple[list[str], Path]] | None = None,
        clear_login: Callable[[], tuple[bool, str]] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.cfg = cfg
        self._log_dir = log_dir
        self._login_provider = login_provider
        self._clear_login = clear_login

        # 字段路径 -> (读控件值的函数, 写控件值的函数)
        self._fields: dict[str, tuple[Callable[[], Any], Callable[[Any], None]]] = {}

        self.setWindowTitle("设置")
        self.resize(560, 620)
        self._build_ui()
        self._load_values()

    # --- 搭界面 -------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        tabs = QTabWidget(self)
        tabs.addTab(self._page_save(), "保存")
        tabs.addTab(self._page_screen(), "截图")
        tabs.addTab(self._page_detector(), "自动识别")
        tabs.addTab(self._page_browser(), "浏览器")
        tabs.addTab(self._page_log(), "日志")
        root.addWidget(tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setText("恢复默认值")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            self._restore_defaults
        )
        root.addWidget(buttons)

    # 下面这几个 _add_* 是给每个页面用的小工具：建控件、登记读写函数、加说明

    def _add_check(self, form: QFormLayout, path: str, label: str, hint: str) -> None:
        box = QCheckBox(label)
        self._fields[path] = (box.isChecked, box.setChecked)
        form.addRow(box)
        form.addRow(_hint(hint))

    def _add_int(
        self, form: QFormLayout, path: str, label: str, hint: str,
        lo: int, hi: int, step: int = 1, suffix: str = "",
    ) -> None:
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setSingleStep(step)
        if suffix:
            spin.setSuffix(suffix)
        self._fields[path] = (spin.value, lambda v, s=spin: s.setValue(int(v)))
        form.addRow(label, spin)
        form.addRow(_hint(hint))

    def _add_float(
        self, form: QFormLayout, path: str, label: str, hint: str,
        lo: float, hi: float, step: float = 0.05, decimals: int = 2,
    ) -> None:
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setSingleStep(step)
        spin.setDecimals(decimals)
        self._fields[path] = (spin.value, lambda v, s=spin: s.setValue(float(v)))
        form.addRow(label, spin)
        form.addRow(_hint(hint))

    def _page_save(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        row = QHBoxLayout()
        self.root_edit = QLineEdit()
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_root)
        row.addWidget(self.root_edit, 1)
        row.addWidget(browse)
        holder = QWidget()
        holder.setLayout(row)
        self._fields["save.root"] = (
            self.root_edit.text,
            lambda v: self.root_edit.setText(str(v)),
        )
        form.addRow("保存到", holder)
        form.addRow(_hint("所有图片和视频都存到这个目录下面。目录不存在会自动创建。"))

        self._add_check(form, "save.split_by_domain", "网页抓的图按网站分文件夹",
                        "开启后是 <保存目录>\\pixiv.net\\2026-08-31\\，关掉就全堆在一起。")
        self._add_check(form, "save.split_by_date", "按日期分文件夹",
                        "开启后每天一个子文件夹。文件多了之后很有用。")
        self._add_check(form, "save.dedupe_by_md5", "内容重复的自动跳过",
                        "按文件内容判断，不看文件名。同一张图存过就不会再存第二遍；"
                        "你把原文件删掉之后，再存同样的内容会正常保存。")
        return page

    def _page_screen(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.hotkey_edit = QLineEdit()
        self._fields["hotkey.capture"] = (
            self.hotkey_edit.text,
            lambda v: self.hotkey_edit.setText(str(v)),
        )
        form.addRow("框选热键", self.hotkey_edit)
        form.addRow(_hint(
            "修饰键要写成 <ctrl> <alt> <shift>，字母直接写。例如 <ctrl>+<alt>+s。\n"
            "保存后立刻生效，不用重启。热键被别的软件占用时按了会没反应，"
            "换一个组合试试。"
        ))

        self.submode_combo = QComboBox()
        self.submode_combo.addItem("手动框选", "manual")
        self.submode_combo.addItem("自动识别", "auto")
        self._fields["screen.default_submode"] = (
            lambda: self.submode_combo.currentData(),
            lambda v: self.submode_combo.setCurrentIndex(
                max(0, self.submode_combo.findData(str(v)))
            ),
        )
        form.addRow("按热键后默认进入", self.submode_combo)
        form.addRow(_hint("不管选哪个，进去之后都能按 Tab 随时切换。"))

        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 255)
        self.opacity_value = QLabel("")
        self.opacity_slider.valueChanged.connect(
            lambda v: self.opacity_value.setText(str(v))
        )
        row = QHBoxLayout()
        row.addWidget(self.opacity_slider, 1)
        row.addWidget(self.opacity_value)
        holder = QWidget()
        holder.setLayout(row)
        self._fields["screen.mask_opacity"] = (
            self.opacity_slider.value,
            lambda v: self.opacity_slider.setValue(int(v)),
        )
        form.addRow("遮罩压暗程度", holder)
        form.addRow(_hint("0 = 完全不压暗，255 = 全黑。默认 120。"))

        self._add_int(form, "screen.edge_snap_px", "边缘吸附距离",
                      "拖到离屏幕边缘这么近时自动贴到边上。设 0 关闭。"
                      "150% 缩放下靠手是点不准最后一个像素的，所以默认留了 2。",
                      0, 20, 1, " 逻辑像素")
        self._add_int(form, "screen.min_selection_px", "最小选区",
                      "边长小于这个值的选区当成误触，直接忽略。",
                      1, 100, 1, " 物理像素")
        return page

    def _page_detector(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        form.addRow(_hint(
            "自动模式是「指哪算哪」：鼠标移到哪，就分析哪一块，滚轮换大小，点击选中。\n"
            "所以下面这些一般不用动——框大小不对，滚一下滚轮比调参数快得多。\n\n"
            "真要调的话：\n"
            "  整片区域都框不出来 -> 把两个「边缘检测」阈值调小\n"
            "  一张图老是被切成两半 -> 把「模糊强度」调大（5 -> 7 -> 9）"
        ))

        self._add_int(form, "detector.canny_low", "边缘检测 · 低阈值",
                      "调低能检出更淡的边界。程序会在这个值上下再自动派生两组一起跑，"
                      "所以不用纠结调到多少才刚好。", 1, 500, 10)
        self._add_int(form, "detector.canny_high", "边缘检测 · 高阈值",
                      "一般设成低阈值的 2~3 倍。", 2, 1000, 10)
        self._add_int(form, "detector.blur_kernel", "模糊强度",
                      "必须是奇数（填了偶数会自动加 1）。调大能忽略图片内部的纹理，"
                      "减少「一张图被切成两半」；但太大会让边界变模糊。",
                      1, 31, 2)
        self._add_int(form, "detector.min_side", "最小边长",
                      "比这还小的区域当噪声丢掉。", 4, 400, 4, " 像素")
        self._add_float(form, "detector.max_area_ratio", "最大占屏比例",
                        "候选框最大能占屏幕多大。1.0 表示允许整屏。",
                        0.05, 1.0, 0.02)
        self._add_float(form, "detector.level_gap", "层级间隔",
                        "相邻两层至少要差这么多面积（0.15 = 15%）。"
                        "调小 -> 滚轮档位更细但要多滚几下；调大 -> 一下跳很多。",
                        0.0, 0.9, 0.05)
        return page

    def _page_browser(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self._add_int(form, "browser.min_width", "最小宽度",
                      "扫描时把比这窄的图筛掉。尺寸未知的图不受影响，一律保留。",
                      0, 4000, 50, " 像素")
        self._add_int(form, "browser.min_height", "最小高度",
                      "同上，按高度筛。", 0, 4000, 50, " 像素")
        self._add_int(form, "browser.concurrency", "同时下载数",
                      "同时下载几个文件。调太高对目标网站不礼貌，也更容易被限流。",
                      1, 8, 1)
        self._add_int(form, "browser.request_interval_ms", "每个请求间隔",
                      "两次请求之间等一会儿，避免给网站造成压力。",
                      0, 5000, 50, " 毫秒")
        self._add_int(form, "browser.retry", "失败重试次数",
                      "下载失败后再试几次。", 0, 5, 1)
        self._add_check(form, "browser.load_thumbnails", "扫描后加载缩略图预览",
                        "关掉之后网格里只显示尺寸和格式文字，扫描会快很多。"
                        "网络慢的时候建议关掉。")

        self.btn_login = QPushButton("查看登录状态…")
        self.btn_login.clicked.connect(self._show_login)
        if self._login_provider is None:
            self.btn_login.setEnabled(False)
            self.btn_login.setToolTip("先打开主窗口再来看")
        form.addRow(self.btn_login)
        form.addRow(_hint("看看受控浏览器里哪些网站存了 cookie（也就是登录过），"
                          "也可以在那里清除全部登录数据。"))
        return page

    def _page_log(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.level_combo = QComboBox()
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self.level_combo.addItem(level, level)
        self._fields["log.level"] = (
            lambda: self.level_combo.currentData(),
            lambda v: self.level_combo.setCurrentIndex(
                max(0, self.level_combo.findData(str(v).upper()))
            ),
        )
        form.addRow("日志详细程度", self.level_combo)
        form.addRow(_hint(
            "排查问题时改成 DEBUG，能看到坐标换算、网络请求的每一步细节。\n"
            "平时用 INFO 就够了。保存后立刻生效。"
        ))

        self._add_int(form, "log.keep_days", "日志保留天数",
                      "超过这个天数的旧日志会在启动时自动删掉。", 1, 365, 1, " 天")

        btn = QPushButton("打开日志目录")
        btn.clicked.connect(self._open_log_dir)
        btn.setEnabled(self._log_dir is not None)
        form.addRow(btn)
        form.addRow(_hint("出问题的时候，把这个目录里当天的日志文件发给我。"))
        return page

    # --- 读写 ---------------------------------------------------------------

    def _load_values(self) -> None:
        """把配置里的值填进各个控件。"""
        for path, (_, setter) in self._fields.items():
            value = self.cfg.get(path)
            if value is None:
                continue
            try:
                setter(value)
            except (TypeError, ValueError) as e:
                log.warning("设置项 %s 的值 %r 填不进界面（%s），跳过", path, value, e)
        self.opacity_value.setText(str(self.opacity_slider.value()))

    def _restore_defaults(self) -> None:
        """
        把界面上的值全部还原成出厂设置。

        只改界面不落盘——你还可以点「取消」反悔。
        """
        from config import DEFAULTS

        answer = QMessageBox.question(
            self, "恢复默认值",
            "把所有设置恢复成出厂默认值？\n（这一步只改界面，点「保存」才会真正写入。）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        for path, (_, setter) in self._fields.items():
            node: Any = DEFAULTS
            for part in path.split("."):
                if not isinstance(node, dict) or part not in node:
                    node = None
                    break
                node = node[part]
            if node is not None:
                setter(node)
        self.opacity_value.setText(str(self.opacity_slider.value()))

    def _on_save(self) -> None:
        """
        校验 -> 写盘 -> 发 applied 信号。

        什么情况会失败：
            热键格式不对        -> 弹窗提示，不保存（不然热键会彻底失灵）
            保存目录是空的      -> 弹窗提示，不保存
            config.json 写不进去 -> 弹窗显示原因，不关窗口
        """
        new_values: dict[str, Any] = {}
        for path, (getter, _) in self._fields.items():
            new_values[path] = getter()

        root = str(new_values.get("save.root", "")).strip()
        if not root:
            QMessageBox.warning(self, "保存目录不能为空", "请填一个保存目录，例如 D:\\MediaGrabber")
            return
        new_values["save.root"] = root

        hotkey = str(new_values.get("hotkey.capture", "")).strip()
        error = validate_hotkey(hotkey)
        if error:
            QMessageBox.warning(self, "热键格式不对", error)
            return
        new_values["hotkey.capture"] = hotkey

        lo = int(new_values.get("detector.canny_low", 50))
        hi = int(new_values.get("detector.canny_high", 150))
        if hi <= lo:
            QMessageBox.warning(
                self, "边缘检测阈值不合理",
                f"高阈值（{hi}）必须大于低阈值（{lo}）。一般设成低阈值的 2~3 倍。",
            )
            return

        changed: dict[str, tuple[Any, Any]] = {}
        for path, value in new_values.items():
            old = self.cfg.get(path)
            if old != value:
                changed[path] = (old, value)
                self.cfg.set(path, value, autosave=False)

        if not changed:
            self.accept()
            return

        try:
            self.cfg.save()
        except OSError as e:
            log.exception("保存配置失败")
            QMessageBox.critical(
                self, "保存失败",
                f"写 config.json 时出错：{e}\n\n"
                f"文件可能被别的程序占用，或者所在目录没有写权限。",
            )
            return

        log.info("设置已更新：%s", {k: v[1] for k, v in changed.items()})
        self.applied.emit(changed)
        self.accept()

    # --- 按钮 ---------------------------------------------------------------

    def _browse_root(self) -> None:
        current = self.root_edit.text().strip() or "D:\\"
        chosen = QFileDialog.getExistingDirectory(self, "选择保存目录", current)
        if chosen:
            # QFileDialog 返回的是正斜杠，转成 Windows 习惯的反斜杠
            self.root_edit.setText(str(Path(chosen)))

    def _open_log_dir(self) -> None:
        import os

        if self._log_dir is None:
            return
        try:
            os.startfile(str(self._log_dir))
        except OSError as e:
            QMessageBox.warning(self, "打不开日志目录", str(e))

    def _show_login(self) -> None:
        if self._login_provider is None:
            return
        try:
            domains, profile_path = self._login_provider()
        except Exception as e:
            log.exception("读取登录状态失败")
            QMessageBox.warning(self, "读不到登录状态", str(e))
            return

        dialog = LoginStateDialog(domains, profile_path, self)
        if self._clear_login is not None:
            dialog.clear_requested.connect(self._do_clear_login)
        dialog.exec()

    def _do_clear_login(self) -> None:
        if self._clear_login is None:
            return
        ok, message = self._clear_login()
        if ok:
            QMessageBox.information(self, "已清除", message)
        else:
            QMessageBox.critical(self, "清除失败", message)


def validate_hotkey(combo: str) -> str | None:
    """
    检查热键字符串能不能被 pynput 解析。

    参数：combo —— 例如 "<ctrl>+<alt>+s"

    返回：没问题返回 None；有问题返回一段给用户看的中文说明。

    为什么一定要校验：热键格式写错时 pynput 是**静默失效**的，
    表现就是「按了没反应」，而且不会有任何报错。与其让你事后困惑，
    不如在点保存的这一刻就拦下来。
    """
    combo = (combo or "").strip()
    if not combo:
        return "热键不能为空。例如：<ctrl>+<alt>+s"

    try:
        from pynput import keyboard
    except ImportError:
        # 没装 pynput 是另一个问题，这里不拦着保存
        log.warning("没装 pynput，跳过热键格式校验")
        return None

    try:
        parsed = keyboard.HotKey.parse(combo)
    except (ValueError, KeyError) as e:
        return (
            f"「{combo}」解析不了：{e}\n\n"
            f"修饰键要用尖括号包起来，各键之间用加号连接。\n"
            f"正确的例子：<ctrl>+<alt>+s   <ctrl>+<shift>+a   <alt>+q"
        )
    if len(parsed) < 2:
        return (
            f"「{combo}」只有一个键，太容易误触了。\n"
            f"请至少加一个修饰键，例如 <ctrl>+<alt>+s"
        )
    return None
