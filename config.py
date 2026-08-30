"""
配置读写封装。

设计要点：
1. 所有字段一次性定义完整（含阶段 2/3 才用到的），避免后续阶段反复改结构，
   导致你已经改过的 config.json 失效。
2. config.json 损坏、字段缺失、类型不对时，一律降级到默认值并写日志警告，绝不崩溃。
3. 首次运行自动生成 config.json。
"""

from __future__ import annotations

import copy
import json
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def app_dir() -> Path:
    """
    返回程序所在目录。

    开发时是本文件所在目录；被 PyInstaller 打包成 exe 后（阶段 4）是 exe 所在目录。
    所有相对路径（config.json / logs / chrome-profile）都以它为基准。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = app_dir() / "config.json"

# ---------------------------------------------------------------------------
# 默认配置。改这里就是改「出厂设置」。
# 用户的 config.json 只是覆盖层，缺什么字段都会从这里补齐。
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "save": {
        # 保存根目录
        "root": "D:\\MediaGrabber",
        # 浏览器模式是否按域名建子文件夹
        "split_by_domain": True,
        # 是否按日期（YYYY-MM-DD）建子文件夹
        "split_by_date": True,
        # 通用模式（屏幕截图）的固定子目录名
        "screen_subdir": "Screen",
        # 是否按文件内容 MD5 去重
        "dedupe_by_md5": True,
        # 文件名最大长度（含扩展名）
        "max_filename_length": 100,
    },
    "hotkey": {
        # pynput 格式的热键字符串。修饰键要用尖括号包起来。
        # 例：<ctrl>+<alt>+s    <ctrl>+<shift>+a    <alt>+q
        "capture": "<ctrl>+<alt>+s",
    },
    "screen": {
        # 默认子模式：manual（手动框选）/ auto（自动识别，阶段 3 才实现）
        "default_submode": "manual",
        # 选区边框颜色
        "selection_color": "#3B82F6",
        # 未选中区域的压暗程度，0（完全不压暗）~ 255（全黑）
        "mask_opacity": 120,
        # 边长小于这个物理像素数的选区视为误触，直接忽略
        "min_selection_px": 4,
        # 拖到离屏幕边缘这么近（逻辑像素）时自动吸附到边缘。设 0 可关闭。
        "edge_snap_px": 2,
    },
    "detector": {  # 阶段 3 · 自动识别，全部阈值可调
        "min_area": 10000,
        "min_aspect": 0.2,
        "max_aspect": 5.0,
        "min_rectangularity": 0.60,
        "canny_low": 50,
        "canny_high": 150,
        "blur_kernel": 5,
        "nms_iou": 0.80,
    },
    "browser": {  # 阶段 2 · 浏览器模式
        "debug_port": 9222,
        "profile_dir": "chrome-profile",
        "chrome_path": "",            # 留空则自动探测（注册表 + 常见路径）
        "min_width": 200,
        "min_height": 200,
        "concurrency": 3,
        "request_interval_ms": 200,
        "retry": 2,
        "stream_threshold_mb": 20,    # 超过这个体积改走 httpx 流式下载
        "blob_max_mb": 50,            # blob: 走 base64 回传的体积上限
        "scan_stale_minutes": 5,      # 扫描结果超过这么久就提示可能已过期
        "first_run_hint_shown": False,
        # 缩略图会额外产生一次请求（多数情况命中浏览器缓存，很快）。
        # 网络很慢又不在意预览时，可以关掉，网格里只显示尺寸和格式文字。
        "load_thumbnails": True,
        "thumbnail_limit": 200,       # 最多给前多少条抓缩略图
        "thumbnail_concurrency": 4,
    },
    "log": {
        "level": "INFO",              # DEBUG / INFO / WARNING / ERROR
        "keep_days": 30,
    },
    "ui": {
        "toast_duration_ms": 2000,
    },
}


def _deep_merge(defaults: dict, loaded: Any) -> tuple[dict, list[str]]:
    """
    把用户配置合并到默认配置上，逐字段做类型校验。

    参数：
        defaults —— 默认配置模板，决定有哪些字段、每个字段该是什么类型
        loaded   —— 从 config.json 读出来的东西，可能是任何类型（文件被人改坏了）

    返回：
        (合并后的配置, 出问题的字段路径列表)

    规则：用户配置里多出来的字段直接丢弃；类型对不上的字段退回默认值并记录下来，
    调用方负责把这些字段名写进日志。
    """
    problems: list[str] = []

    def merge(d: dict, u: Any, prefix: str) -> dict:
        out = copy.deepcopy(d)
        if not isinstance(u, dict):
            if u is not None:
                problems.append(prefix.rstrip(".") or "<根节点>")
            return out
        for key, default_value in d.items():
            if key not in u:
                continue  # 缺字段：静默用默认值，这是正常的版本升级情况
            user_value = u[key]
            path = f"{prefix}{key}"
            if isinstance(default_value, dict):
                out[key] = merge(default_value, user_value, path + ".")
            elif isinstance(default_value, bool):
                # bool 必须排在 int 前面判断，因为 Python 里 bool 是 int 的子类
                if isinstance(user_value, bool):
                    out[key] = user_value
                else:
                    problems.append(path)
            elif isinstance(default_value, (int, float)):
                if isinstance(user_value, (int, float)) and not isinstance(user_value, bool):
                    out[key] = type(default_value)(user_value)
                else:
                    problems.append(path)
            elif isinstance(default_value, str):
                if isinstance(user_value, str):
                    out[key] = user_value
                else:
                    problems.append(path)
            else:
                out[key] = user_value
        return out

    return merge(defaults, loaded, ""), problems


class Config:
    """
    配置对象。用点号路径读写，例如 CONFIG.get("save.root")。

    什么情况会失败：
      - config.json 是坏 JSON  -> 全量退回默认值，坏文件备份成 config.json.bak
      - 某个字段类型不对        -> 只有该字段退回默认值，其余保留
      - 磁盘只读导致保存失败    -> save() 抛 OSError，由调用方决定怎么提示用户
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else CONFIG_PATH
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        """从磁盘读配置。文件不存在就用默认值并立刻写一份出来。"""
        if not self.path.exists():
            log.info("未找到 config.json，使用默认配置并生成一份：%s", self.path)
            self._data = copy.deepcopy(DEFAULTS)
            try:
                self.save()
            except OSError as e:
                log.error("生成默认 config.json 失败：%s", e)
            return

        try:
            raw = self.path.read_text(encoding="utf-8")
            loaded = json.loads(raw)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            log.error("config.json 读取失败（%s），本次使用默认配置", e)
            backup = self.path.with_suffix(".json.bak")
            try:
                self.path.replace(backup)
                log.warning("已把损坏的配置文件备份为：%s", backup)
            except OSError as be:
                log.warning("备份损坏的配置文件失败：%s", be)
            self._data = copy.deepcopy(DEFAULTS)
            try:
                self.save()
            except OSError as se:
                log.error("重建 config.json 失败：%s", se)
            return

        merged, problems = _deep_merge(DEFAULTS, loaded)
        for p in problems:
            log.warning("配置项 %s 的类型不正确，已退回默认值", p)
        self._data = merged

    def save(self) -> None:
        """写回磁盘。先写临时文件再改名，避免写到一半留下一个损坏的配置。"""
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def get(self, dotted: str, default: Any = None) -> Any:
        """按 save.root 这样的点号路径取值。路径不存在时返回 default。"""
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any, autosave: bool = True) -> None:
        """按点号路径写值。父级节点不存在会自动创建。"""
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        if autosave:
            self.save()

    @property
    def data(self) -> dict[str, Any]:
        """拿到完整配置字典的副本。改这个副本不会影响真正的配置。"""
        return copy.deepcopy(self._data)

    # --- 常用派生路径 -------------------------------------------------------

    def save_root(self) -> Path:
        """保存根目录。配置里被清空时退回默认值，不返回一个空路径。"""
        raw = str(self.get("save.root") or "").strip()
        return Path(raw) if raw else Path(DEFAULTS["save"]["root"])

    def profile_path(self) -> Path:
        """受控 Chrome 的 user-data-dir 绝对路径（阶段 2 用）。"""
        raw = str(self.get("browser.profile_dir") or "chrome-profile")
        p = Path(raw)
        return p if p.is_absolute() else app_dir() / p


# 全局单例。其他模块直接 from config import CONFIG 使用。
CONFIG = Config()
