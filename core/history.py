"""
保存记录。

存成 JSONL（每行一个 JSON 对象）而不是数据库，理由：
  - 出问题时你可以直接用记事本打开看，不需要任何工具
  - 追加写入不会破坏已有内容，程序崩了最多丢最后一行

除了给阶段 3 的历史面板用，它还承担一个阶段 1 就需要的职责：
维护 MD5 索引，用来实现 CLAUDE.md 第 5 节要求的「按文件内容去重」。

文件位置：<程序目录>\\history.jsonl
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

HISTORY_FILENAME = "history.jsonl"

# 单行超过这个长度就认为这行坏了，跳过（防止某行被写坏后无限吃内存）
_MAX_LINE_LEN = 8192


class History:
    """
    保存记录 + MD5 索引。

    线程安全：所有公开方法都加了锁，下载线程池可以并发调用。

    什么情况会失败：
      - history.jsonl 不可读 -> 当作空记录，写日志警告，程序继续
      - 某一行是坏 JSON      -> 跳过那一行，写日志警告
      - 写入失败             -> record() 抛 OSError，由调用方决定怎么提示
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        # md5 -> 文件路径字符串。同一个 md5 只记最后一次保存的位置。
        self._md5_index: dict[str, str] = {}
        self._count = 0
        self.load()

    # --- 读 -----------------------------------------------------------------

    def load(self) -> None:
        """把整个 history.jsonl 读进内存，重建 MD5 索引。启动时调用一次。"""
        with self._lock:
            self._md5_index.clear()
            self._count = 0
            if not self.path.exists():
                log.info("尚无历史记录文件，将在第一次保存时创建：%s", self.path)
                return
            bad_lines = 0
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or len(line) > _MAX_LINE_LEN:
                            if line:
                                bad_lines += 1
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            bad_lines += 1
                            continue
                        if not isinstance(entry, dict):
                            bad_lines += 1
                            continue
                        md5 = entry.get("md5")
                        path = entry.get("path")
                        if isinstance(md5, str) and isinstance(path, str):
                            self._md5_index[md5] = path
                        self._count += 1
            except OSError as e:
                log.warning("读取历史记录失败（%s），本次按空记录处理", e)
                return
            if bad_lines:
                log.warning("历史记录里有 %d 行内容损坏，已跳过", bad_lines)
            log.info("已载入 %d 条历史记录，MD5 索引 %d 条", self._count, len(self._md5_index))

    def find_duplicate(self, md5: str) -> Path | None:
        """
        按内容 MD5 查这个文件之前是不是已经存过了。

        返回：已存在的文件路径；没存过、或者存过但文件已经被你删掉了，返回 None。

        「被删掉就不算重复」是故意的——你既然删了，说明还想再要一份。
        """
        with self._lock:
            recorded = self._md5_index.get(md5)
        if not recorded:
            return None
        p = Path(recorded)
        if p.exists():
            return p
        # 文件没了，把这条索引清掉，免得每次都白查一遍
        with self._lock:
            self._md5_index.pop(md5, None)
        return None

    def entries(self, limit: int | None = None, newest_first: bool = True) -> list[dict[str, Any]]:
        """
        读出历史记录条目，给阶段 3 的历史面板用。

        参数 limit 为 None 表示全读。文件很大时只关心最近几条，传 limit 更快。
        """
        rows: list[dict[str, Any]] = []
        if not self.path.exists():
            return rows
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or len(line) > _MAX_LINE_LEN:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(entry, dict):
                        rows.append(entry)
        except OSError as e:
            log.warning("读取历史记录失败：%s", e)
            return rows
        if newest_first:
            rows.reverse()
        return rows[:limit] if limit else rows

    def __len__(self) -> int:
        with self._lock:
            return self._count

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.entries())

    # --- 写 -----------------------------------------------------------------

    def record(
        self,
        *,
        path: Path,
        md5: str,
        size: int,
        kind: str,
        source: str = "",
        width: int = 0,
        height: int = 0,
    ) -> None:
        """
        追加一条保存记录。

        参数：
            path   —— 存到哪了
            md5    —— 文件内容的 MD5，用于去重
            size   —— 字节数
            kind   —— "screen"（屏幕截图）或 "browser"（网页抓取）
            source —— 来源说明。浏览器模式填原始 URL，通用模式填屏幕名。
            width/height —— 图片尺寸，拿不到就填 0

        什么情况会失败：磁盘满、目录不可写 -> 抛 OSError。
        调用方要注意：文件其实已经存好了，只是记录没写上，别因此告诉用户保存失败。
        """
        entry = {
            "time": _dt.datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "path": str(path),
            "md5": md5,
            "size": int(size),
            "width": int(width),
            "height": int(height),
            "source": str(source),
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._md5_index[md5] = str(path)
            self._count += 1
