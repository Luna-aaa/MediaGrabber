"""
统一的落盘逻辑，两种模式共用。

阶段 1 只需要 save_bytes()：拿到一段字节，按 CLAUDE.md 第 5 节的规则存到磁盘。
阶段 2 会在这个文件里补充网络下载（page.request / httpx），但**落盘这一步永远走
save_bytes()**，保证两种模式的去重、重名避让、目录规则完全一致。

落盘顺序（顺序不能改）：
    算 MD5 -> 查重 -> 建目录 -> 找不冲突的文件名 -> 写 .tmp -> 改名 -> 记历史
「先写 .tmp 再改名」是为了保证：万一写到一半断电或崩溃，你的目录里只会多一个
.tmp 垃圾文件，而不会多一张打不开的半截图片。
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from core import naming
from core.history import History

log = logging.getLogger(__name__)

TMP_SUFFIX = ".mgtmp"


@dataclass
class SaveResult:
    """
    一次落盘的结果。

    三种结局，靠 ok / skipped 区分：
        ok=True,  skipped=False -> 存成功了，path 是文件位置
        ok=True,  skipped=True  -> 内容重复，没存，duplicate_of 是已有文件的位置
        ok=False                -> 失败了，error 是给用户看的中文原因
    """

    ok: bool
    path: Path | None = None
    skipped: bool = False
    duplicate_of: Path | None = None
    md5: str = ""
    size: int = 0
    error: str | None = None

    def message(self) -> str:
        """一句话总结，直接可以显示在提示条上。"""
        if not self.ok:
            return f"保存失败：{self.error}"
        if self.skipped:
            return f"已存在，已跳过（原文件：{self.duplicate_of}）"
        return f"已保存：{self.path}"


def md5_bytes(data: bytes) -> str:
    """算一段字节的 MD5 十六进制字符串。用于内容去重。"""
    return hashlib.md5(data).hexdigest()


# ---------------------------------------------------------------------------
# 落盘的三个步骤，拆出来给两条下载路径共用
#
# 为什么要拆：小文件是「先全读进内存再写」，大文件（视频）是「边下边写」，
# 两条路径的写入方式不同，但**去重规则、重名避让、历史记录必须完全一致**。
# 拆成公共步骤就不会出现「图片会去重、视频不去重」这种行为不一致。
# ---------------------------------------------------------------------------


def check_duplicate(history: History | None, digest: str, dedupe: bool = True) -> Path | None:
    """查内容是否已经存过。返回已有文件路径，或 None。"""
    if not dedupe or history is None:
        return None
    try:
        return history.find_duplicate(digest)
    except Exception:
        log.exception("查重时出错，本次跳过去重直接保存")
        return None


def prepare_target(directory: Path, filename: str, max_filename_length: int = 100) -> Path:
    """
    建好目录并算出一个不冲突的目标路径。

    什么情况会失败：目录建不出来、同名文件超过 9999 个 -> 抛 OSError。
    """
    directory = Path(directory)
    naming.ensure_dir(directory)
    safe_name = naming.sanitize_filename(filename, max_len=max_filename_length)
    return naming.unique_path(directory, safe_name)


def commit_record(
    history: History | None,
    *,
    path: Path,
    digest: str,
    size: int,
    kind: str,
    source: str = "",
    width: int = 0,
    height: int = 0,
) -> None:
    """
    写历史记录。

    注意：调用到这里时文件**已经存好了**。记录写失败只影响去重和历史面板，
    绝不能因此告诉用户「保存失败」，所以这里把异常吃掉、只写日志。
    """
    if history is None:
        return
    try:
        history.record(
            path=path, md5=digest, size=size, kind=kind,
            source=source, width=width, height=height,
        )
    except OSError as e:
        log.error("文件已保存，但写历史记录失败：%s", e)


def save_bytes(
    data: bytes,
    directory: Path,
    filename: str,
    *,
    history: History | None = None,
    dedupe: bool = True,
    max_filename_length: int = 100,
    kind: str = "screen",
    source: str = "",
    width: int = 0,
    height: int = 0,
) -> SaveResult:
    """
    把一段字节存成文件。这是两种模式唯一的落盘入口。

    参数：
        data                —— 要写的字节
        directory           —— 目标目录（不存在会自动创建）
        filename            —— 建议的文件名，内部会先做清洗
        history             —— 历史记录对象；传了才能去重和记录
        dedupe              —— 是否按 MD5 去重
        max_filename_length —— 文件名长度上限
        kind / source / width / height —— 只用于写历史记录

    返回：SaveResult，绝不抛异常。所有失败都转成 ok=False + 中文 error。

    什么情况会失败：
        - data 是空的（网络返回了 0 字节 / 选区尺寸为 0）
        - 盘符不存在、没写权限、磁盘满
        - 目标目录被同名文件占着
        - 同名文件超过 9999 个
    """
    if not data:
        return SaveResult(ok=False, error="内容是空的（0 字节），没有东西可以保存")

    digest = md5_bytes(data)
    size = len(data)

    # --- 1. 内容去重 --------------------------------------------------------
    existing = check_duplicate(history, digest, dedupe)
    if existing is not None:
        log.info("内容重复（MD5 %s），跳过保存。已有文件：%s", digest[:8], existing)
        return SaveResult(
            ok=True, skipped=True, duplicate_of=existing, md5=digest, size=size
        )

    # --- 2. 建目录 + 定文件名 -----------------------------------------------
    try:
        target = prepare_target(directory, filename, max_filename_length)
    except (OSError, FileExistsError) as e:
        log.error("确定保存位置失败：%s", e)
        return SaveResult(ok=False, md5=digest, size=size, error=str(e))

    # --- 4. 先写临时文件，再原子改名 ----------------------------------------
    tmp = target.with_name(target.name + TMP_SUFFIX)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())   # 确保真的落到磁盘，而不是只在系统缓存里
        os.replace(tmp, target)    # 同一个盘上的改名是原子操作
    except OSError as e:
        # 清理半截的临时文件，别在用户目录里留垃圾
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            log.warning("临时文件 %s 清理失败，可以手动删掉", tmp)
        drive = target.drive or "(无盘符)"
        msg = (
            f"写入 {target} 失败：{e}。"
            f"请检查 {drive} 盘剩余空间、写入权限，以及该文件是否正被别的程序占用。"
        )
        log.error(msg)
        return SaveResult(ok=False, md5=digest, size=size, error=msg)

    log.info("已保存 %s（%d 字节，MD5 %s）", target, size, digest[:8])

    # --- 5. 记历史 ----------------------------------------------------------
    commit_record(
        history, path=target, digest=digest, size=size,
        kind=kind, source=source, width=width, height=height,
    )
    return SaveResult(ok=True, path=target, md5=digest, size=size)


def cleanup_stale_temp_files(directory: Path) -> int:
    """
    清掉目录里遗留的 .mgtmp 临时文件（上次崩溃留下的）。

    返回清掉的个数。目录不存在或不可读时返回 0，不报错。
    """
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    removed = 0
    try:
        for p in directory.glob(f"*{TMP_SUFFIX}"):
            try:
                p.unlink()
                removed += 1
            except OSError as e:
                log.warning("清理临时文件 %s 失败：%s", p, e)
    except OSError as e:
        log.warning("扫描临时文件失败：%s", e)
    if removed:
        log.info("清理了 %d 个上次遗留的临时文件", removed)
    return removed
