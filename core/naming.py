"""
文件名清洗、目录规划、重名避让。

CLAUDE.md 第 5 节的保存规则全部实现在这里，两种模式共用同一套逻辑：

    浏览器模式   <根目录>\\<域名>\\<YYYY-MM-DD>\\
    通用模式     <根目录>\\Screen\\<YYYY-MM-DD>\\

「按域名分」「按日期分」都是 config.json 里的开关，关掉就少一层目录。
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)

# Windows 文件名里不允许出现的字符（CLAUDE.md 第 5 节列的那批）
_ILLEGAL_CHARS = r'\/:*?"<>|'
_ILLEGAL_RE = re.compile(f"[{re.escape(_ILLEGAL_CHARS)}]")
# ASCII 控制字符也不允许
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Windows 保留的设备名。叫这些名字的文件建不出来，哪怕带扩展名也不行。
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 重名避让最多尝试多少次
_MAX_DEDUPE_ATTEMPTS = 9999


def sanitize_filename(name: str, max_len: int = 100, default: str = "untitled") -> str:
    """
    把任意字符串洗成一个 Windows 上合法的文件名。

    参数：
        name    —— 原始名字，可以带扩展名
        max_len —— 最终长度上限（含扩展名），超长时截断主干部分、保留扩展名
        default —— 洗完啥也不剩时用的兜底名字

    返回：合法文件名（不含目录部分）。

    处理内容：
      1. 非法字符 \\ / : * ? " < > | 和控制字符 -> 下划线
      2. 去掉结尾的空格和点（Windows 会自己去掉，导致文件名和你以为的不一样）
      3. 撞上 CON / NUL / COM1 这类保留设备名时加下划线前缀
      4. 超长时截断主干，扩展名完整保留
    """
    name = str(name or "").strip()
    name = unquote(name)          # 处理 URL 里的 %E4%B8%AD 之类
    name = _CONTROL_RE.sub("", name)
    name = _ILLEGAL_RE.sub("_", name)
    name = name.strip()

    # 拆出扩展名。扩展名过长（超过 10 个字符）多半不是真扩展名，当成主干处理。
    p = Path(name)
    ext = p.suffix if 0 < len(p.suffix) <= 10 else ""
    stem = name[: len(name) - len(ext)] if ext else name

    stem = stem.rstrip(" .")
    if not stem:
        stem = default

    if stem.upper() in _RESERVED_NAMES:
        stem = "_" + stem

    # 截断：保证 len(stem) + len(ext) <= max_len，且主干至少留 1 个字符
    max_len = max(8, int(max_len))
    allowed_stem = max(1, max_len - len(ext))
    if len(stem) > allowed_stem:
        stem = stem[:allowed_stem].rstrip(" .") or default[:allowed_stem]

    return stem + ext


def domain_from_url(url: str, default: str = "unknown-site") -> str:
    """
    从 URL 里取出域名，用作目录名（阶段 2 的浏览器模式用）。

    去掉 www. 前缀和端口号。取不出来（比如传进来的是 data: 地址）时返回 default。
    """
    try:
        host = urlparse(str(url)).hostname or ""
    except ValueError:
        host = ""
    host = host.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return sanitize_filename(host, max_len=64) if host else default


def build_save_dir(
    root: Path,
    kind: str,
    *,
    domain: str | None = None,
    screen_subdir: str = "Screen",
    split_by_domain: bool = True,
    split_by_date: bool = True,
    when: _dt.datetime | None = None,
) -> Path:
    """
    算出这次该存到哪个目录（只计算路径，不建目录）。

    参数：
        kind            —— "screen"（通用模式）或 "browser"（浏览器模式）
        domain          —— 浏览器模式下的来源域名
        screen_subdir   —— 通用模式的固定子目录名，默认 Screen
        split_by_domain —— 浏览器模式是否按域名分层
        split_by_date   —— 是否按日期分层
        when            —— 用哪个时间算日期目录，默认现在

    返回：目标目录的 Path（可能还不存在）。
    """
    when = when or _dt.datetime.now()
    path = Path(root)

    if kind == "screen":
        path = path / sanitize_filename(screen_subdir or "Screen", max_len=64)
    elif kind == "browser":
        if split_by_domain:
            path = path / domain_from_url(domain or "")
    else:
        raise ValueError(f"未知的保存类型：{kind!r}（只支持 screen / browser）")

    if split_by_date:
        path = path / when.strftime("%Y-%m-%d")

    return path


def ensure_dir(path: Path) -> Path:
    """
    确保目录存在，不存在就一路建出来。

    什么情况会失败：盘符不存在、路径被同名文件占着、没有写权限。
    这三种情况都会抛 OSError，并且异常信息里会写清楚是哪个目录、什么原因，
    绝不静默吞掉——上层负责把它显示到界面上。
    """
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        drive = path.drive or "(无盘符)"
        raise OSError(
            f"无法创建保存目录 {path}：{e}。"
            f"请检查 {drive} 盘是否存在、是否有写入权限、路径是否被同名文件占用。"
        ) from e
    if not path.is_dir():
        raise OSError(f"保存路径 {path} 存在但不是目录，请手动处理掉这个同名文件。")
    return path


def unique_path(directory: Path, filename: str) -> Path:
    """
    在 directory 下为 filename 找一个不冲突的完整路径。

    已存在就依次试 name_1.ext、name_2.ext ……**绝不覆盖已有文件**。

    什么情况会失败：同名文件太多（超过 9999 个）时抛 FileExistsError。
    """
    directory = Path(directory)
    p = Path(filename)
    ext = p.suffix
    stem = filename[: len(filename) - len(ext)] if ext else filename

    candidate = directory / filename
    if not candidate.exists():
        return candidate

    for i in range(1, _MAX_DEDUPE_ATTEMPTS + 1):
        candidate = directory / f"{stem}_{i}{ext}"
        if not candidate.exists():
            return candidate

    raise FileExistsError(
        f"{directory} 下已经有超过 {_MAX_DEDUPE_ATTEMPTS} 个名为 {stem}{ext} 的文件，"
        f"请先清理一下这个目录。"
    )


def filename_from_url(url: str, ext: str, fallback_stem: str = "image") -> str:
    """
    从 URL 里推出一个像样的文件名（浏览器模式用）。

    参数：
        url           —— 资源地址
        ext           —— 已经确定好的扩展名（由 scanner.guess_extension 给出）
        fallback_stem —— URL 里榨不出名字时用的前缀

    返回：清洗过的文件名，一定带扩展名。

    几种情况：
        .../photo_1234.jpg?token=xx   -> photo_1234.jpg
        .../image/abc123              -> abc123.jpg
        .../                          -> image_a1b2c3d4.jpg（用 URL 的哈希兜底）
        data:image/png;base64,...     -> image_a1b2c3d4.png
    """
    if not ext.startswith("."):
        ext = "." + ext

    stem = ""
    if not str(url).startswith("data:"):
        try:
            path = urlparse(str(url)).path
        except ValueError:
            path = ""
        raw = unquote(path.rstrip("/").rsplit("/", 1)[-1]) if path else ""
        # 去掉原有扩展名，统一用传进来的 ext
        p = Path(raw)
        stem = raw[: len(raw) - len(p.suffix)] if 0 < len(p.suffix) <= 10 else raw
        stem = stem.strip()

    if not stem:
        # URL 里没有可用的名字，用地址的哈希做后缀，保证不同资源不同名
        import hashlib
        digest = hashlib.md5(str(url).encode("utf-8", "replace")).hexdigest()[:8]
        stem = f"{fallback_stem}_{digest}"

    return sanitize_filename(stem + ext)


def screen_filename(when: _dt.datetime | None = None, ext: str = ".png") -> str:
    """
    生成通用模式的文件名：screen_20260830_143052.png

    同一秒内连续截多张时靠 unique_path() 加 _1 _2 后缀区分，这里不加毫秒，
    保持文件名短且可读。
    """
    when = when or _dt.datetime.now()
    if not ext.startswith("."):
        ext = "." + ext
    return f"screen_{when.strftime('%Y%m%d_%H%M%S')}{ext}"
