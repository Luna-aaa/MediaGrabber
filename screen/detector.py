"""
点选式图片区域识别（阶段 3，2026-08-31 重做）。

## 为什么推翻了第一版

第一版是「先扫描全屏，用阈值筛出所有像图片的矩形，全画成黄框」。
实测下来三个毛病同时存在：有的图识别不到、有的边界清晰却判断错、
还有一张图被切成两半的。

根因是**整块屏幕共用一套阈值**：
    阈值调高 -> 浅色背景上的浅色图检不出边缘 -> 漏检
    阈值调低 -> 图片内部的明暗分界也被当成边界 -> 一张切两半
    取中间   -> 两种毛病各来一半
这不是参数没调好，是这个思路的天花板——因为它必须在没有任何线索的情况下
猜「这块是不是一张图」。

## 这一版的思路

**用鼠标位置当锚点。** 你把鼠标放在哪，问题就从「屏幕上哪些是图」
变成了「包住这个点的边界在哪」——后者好解得多，而且答案可以立刻画出来给你看。

于是筛选这一步整个不要了：
  - 不筛面积、不筛宽高比、不筛方正程度（这些正是漏检的来源）
  - 用**多组**边缘阈值各跑一遍，全部结果丢进候选池
  - 候选允许重叠、允许嵌套——因为不需要「选出正确的那个」，
    只需要把包住鼠标的都列出来，让你用滚轮挑

两条互补的候选来源：

1. **轮廓法**：Canny -> 闭运算 -> findContours，取每个轮廓的外接矩形。
   对有明确边框的图很准。

2. **投影法**：从鼠标位置向四个方向扫，找第一条「横跨够长的边缘线」。
   专治轮廓没闭合的情况——图的边框缺了一角时，轮廓法会失败，
   投影法照样能找到四条边。用几个不同的强度阈值扫，天然形成由小到大的层级。

3. **颜色漫延法**：从鼠标位置向外扩散，把颜色接近的像素圈进来。
   专治**低对比度**——浅灰底上的浅灰块，灰度只差十几，
   高斯模糊之后梯度更小，Canny 把阈值调到能检出它的程度时，
   满屏文字纹理会先淹没结果。但按颜色一致性判断，这种块反而很好圈。
   纯色块、纯色背景上的图它最拿手；照片那种花花绿绿的内容它不行，
   正好由前两条路负责。

三边的结果合并去重，按面积从小到大排成层级，滚轮就在层级之间切换。
一张图被切成两半时，往上滚一格就是完整的那张。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QRect
from PySide6.QtGui import QImage

log = logging.getLogger(__name__)


@dataclass
class DetectParams:
    """
    识别参数。来自 config.json 的 detector 段，设置面板可改。

    canny_low / canny_high
        边缘检测的基准双阈值。程序会在这个基准上下再派生出两组
        （一组更敏感、一组更保守）一起跑，所以不用纠结调到多少才刚好。
        整体检不到边缘时把两个都调小。
    blur_kernel
        高斯模糊核，必须是奇数。调大能忽略图片内部的细节纹理，
        减少「一张图被切成两半」；但太大会让边界变模糊。
    min_side
        候选框的最小边长。比这还小的当噪声丢掉。
    max_area_ratio
        候选框最大能占屏幕多大。1.0 表示允许整屏。
    level_gap
        相邻两个层级的面积至少要差这么多（0.15 = 15%）。
        差得太少的话，滚轮要滚好几下框才有肉眼可见的变化。
    """

    canny_low: int = 50
    canny_high: int = 150
    blur_kernel: int = 5
    min_side: int = 24
    max_area_ratio: float = 0.98
    level_gap: float = 0.15

    @classmethod
    def from_config(cls, cfg) -> "DetectParams":
        """
        从 config 读参数。任何一项读坏了都退回默认值并写日志，
        绝不因为手改坏了 config.json 就让识别整个不能用。
        """
        def num(key: str, default, cast):
            raw = cfg.get(f"detector.{key}", default)
            try:
                return cast(raw)
            except (TypeError, ValueError):
                log.warning("detector.%s 的值 %r 不是数字，退回默认值 %r", key, raw, default)
                return default

        return cls(
            canny_low=num("canny_low", 50, int),
            canny_high=num("canny_high", 150, int),
            blur_kernel=num("blur_kernel", 5, int),
            min_side=num("min_side", 24, int),
            max_area_ratio=num("max_area_ratio", 0.98, float),
            level_gap=num("level_gap", 0.15, float),
        ).sanitized()

    def sanitized(self) -> "DetectParams":
        """
        把参数夹到合法范围。

        高斯模糊的核必须是**正奇数**，传个 4 进去 OpenCV 直接抛异常——
        与其让识别在运行时炸掉，不如在这儿悄悄修正并写一行日志。
        """
        k = max(1, int(self.blur_kernel))
        if k % 2 == 0:
            log.warning("detector.blur_kernel 必须是奇数，%d 已自动改成 %d", k, k + 1)
            k += 1
        lo = max(1, int(self.canny_low))
        hi = max(lo + 1, int(self.canny_high))
        return DetectParams(
            canny_low=lo,
            canny_high=hi,
            blur_kernel=k,
            min_side=max(4, int(self.min_side)),
            max_area_ratio=min(1.0, max(0.05, float(self.max_area_ratio))),
            level_gap=min(0.9, max(0.0, float(self.level_gap))),
        )

    def threshold_sets(self) -> list[tuple[int, int]]:
        """
        派生出几组边缘阈值一起跑。

        一组阈值不可能同时适配「深色背景上的亮图」和「浅色背景上的浅图」，
        所以干脆都跑一遍，结果全都丢进候选池——反正最后是你用滚轮挑。

        敏感的那组能捞回浅色图（代价是碎片多，但碎片不选就是了）；
        保守的那组给出干净的大轮廓。
        """
        lo, hi = self.canny_low, self.canny_high
        return [
            (max(1, lo // 2), max(2, hi // 2)),   # 敏感：捞浅色边界
            (lo, hi),                              # 基准
            (min(lo * 2, 240), min(hi * 2, 480)),  # 保守：只留强边界
        ]


@dataclass
class RegionIndex:
    """
    一块屏幕预计算好的候选索引。

    进入自动模式时算一次（约 100~200 毫秒，在线程池里），
    之后鼠标每动一下只是查表，不用重新做图像处理。

    字段：
        rects  —— 轮廓法得到的所有候选矩形（本屏物理坐标，没经过任何筛选）
        edges  —— 合并后的边缘图（numpy uint8，0 或 255），投影法要用
        width/height —— 这块屏的物理尺寸
    """

    rects: list[QRect] = field(default_factory=list)
    edges: Any = None
    bgr: Any = None
    width: int = 0
    height: int = 0
    elapsed_ms: float = 0.0

    @property
    def ready(self) -> bool:
        return self.edges is not None and self.width > 0


def qimage_to_bgr(image: QImage):
    """
    把 QImage 转成 OpenCV 用的 BGR numpy 数组，不做任何缩放。

    参数：image —— 冻结画面裁出来的那一块，必须是物理像素

    什么情况会失败：
        - numpy 没装 -> ImportError
        - 图像为空   -> ValueError

    注意 bytesPerLine：Qt 会把每一行补齐到 4 字节对齐，行尾可能有填充字节。
    直接按 width*4 reshape 会在某些尺寸下把图像撕成斜的——必须按 bytesPerLine
    取行，再切掉行尾的填充。

    **结尾那个 copy() 千万不能省。** np.frombuffer(constBits()) 得到的数组
    不拥有内存，只是 QImage 缓冲区上的一个视图。RegionIndex 要把它一直存着
    给颜色漫延法用，而那时候原来的 QImage 可能早就被回收了——
    访问一个已经释放的缓冲区，进程直接以「访问违例」崩掉，
    连异常都抛不出来。多复制这十几 MB 换的是这个。
    """
    import numpy as np

    if image.isNull():
        raise ValueError("图像为空，无法识别")

    img = image
    if img.format() != QImage.Format.Format_RGB32:
        img = img.convertToFormat(QImage.Format.Format_RGB32)

    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * h)
    rows = buf.reshape(h, bpl)[:, : w * 4]
    # 小端机器上 Format_RGB32 在内存里的字节序就是 B,G,R,X，取前三个通道正好是 BGR。
    # copy() 让数组脱离 QImage 的缓冲区，理由见上面的说明。
    return rows.reshape(h, w, 4)[:, :, :3].copy()


def build_region_index(image: QImage, params: DetectParams) -> RegionIndex:
    """
    预计算一块屏幕的候选索引。在线程池里跑，不要在主线程调。

    参数：
        image  —— 该屏的冻结画面（物理像素）
        params —— 识别参数

    返回：RegionIndex。查询用 candidates_at()。

    什么情况会失败：
        - opencv-python / numpy 没装 -> ImportError
        - 图像为空                    -> ValueError

    这里**故意不做任何筛选**。第一版就是栽在筛选上：为了滤掉噪声定的那些
    阈值，同时也把真正想要的图滤掉了。现在有鼠标位置当锚点，噪声候选
    只要不在鼠标底下就永远不会被看到，留着一点代价都没有。
    """
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise ImportError(
            "缺少 opencv-python，无法使用自动识别。请在项目目录下执行："
            "pip install -r requirements.txt"
        ) from e

    t0 = time.perf_counter()
    bgr = qimage_to_bgr(image)
    h, w = bgr.shape[:2]

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (params.blur_kernel, params.blur_kernel), 0)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))

    max_area = w * h * params.max_area_ratio
    min_side = params.min_side
    seen: set[tuple[int, int, int, int]] = set()
    rects: list[QRect] = []
    merged_edges = np.zeros((h, w), dtype=np.uint8)

    for lo, hi in params.threshold_sets():
        edges = cv2.Canny(blurred, lo, hi)
        # 闭运算把断掉的边缘接上。不做这步的话 findContours 得到的是绕着
        # 线条一圈的细环，围不出任何区域。
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
        merged_edges = cv2.bitwise_or(merged_edges, closed)

        contours, _ = cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, cw, ch = cv2.boundingRect(contour)
            if cw < min_side or ch < min_side:
                continue
            if cw * ch > max_area:
                continue
            # 只按坐标去重。不同阈值组常常给出一模一样的框，留一份就够了。
            key = (x, y, cw, ch)
            if key in seen:
                continue
            seen.add(key)
            rects.append(QRect(x, y, cw, ch))

    elapsed = (time.perf_counter() - t0) * 1000
    log.info(
        "候选索引建好：%dx%d 画面，%d 组阈值，得到 %d 个候选矩形，耗时 %.0f ms",
        w, h, len(params.threshold_sets()), len(rects), elapsed,
    )
    # bgr 留着给颜色漫延法用。2560x1600 的画面约 12MB，遮罩一关就释放了。
    return RegionIndex(
        rects=rects, edges=merged_edges, bgr=bgr, width=w, height=h, elapsed_ms=elapsed
    )


# 颜色漫延的搜索窗口边长（物理像素）。
# 不在整屏上漫延有三个原因：一是快，二是从背景点出发会填满整个屏幕，
# 白等半天还得不到有用的结果，三是窗口越大，下面那次内存复制越贵。
# 限制在窗口内，贴到窗口边就知道该放弃了。
_FLOOD_WINDOW = 1200


def _flood_bounds(bgr, x: int, y: int, tolerances, min_side: int) -> list:
    """
    颜色漫延法：从 (x, y) 出发把颜色接近的像素圈起来，返回它们的外接矩形。

    参数：
        bgr        —— 画面（numpy BGR）
        x, y       —— 起点（本屏物理坐标）
        tolerances —— 几个容差值，越大圈得越宽。每个给出一个候选
        min_side   —— 小于这个边长的结果作废

    返回：矩形列表（可能为空）。

    什么时候有用：低对比度的纯色块、纯色背景上的图——
    这些正是 Canny 检不出来的。什么时候没用：照片、渐变、花纹密集的内容，
    那种情况交给轮廓法和投影法。

    用 FLOODFILL_FIXED_RANGE：每个像素和**起点**比颜色，而不是和邻居比。
    和邻居比的话，遇到渐变会一路漫过去，最后圈住半个屏幕。
    """
    import cv2
    import numpy as np

    h, w = bgr.shape[:2]
    if not (0 <= x < w and 0 <= y < h):
        return []

    # 只在鼠标周围的窗口里漫延
    x0 = max(0, x - _FLOOD_WINDOW // 2)
    y0 = max(0, y - _FLOOD_WINDOW // 2)
    x1 = min(w, x0 + _FLOOD_WINDOW)
    y1 = min(h, y0 + _FLOOD_WINDOW)
    roi = np.ascontiguousarray(bgr[y0:y1, x0:x1])
    rh, rw = roi.shape[:2]
    seed = (x - x0, y - y0)

    out = []
    # mask 建一次反复用。每次新建的话，光是申请和清零这块内存就够慢的了。
    mask = np.zeros((rh + 2, rw + 2), np.uint8)
    flags = (
        4                              # 四邻域，不走对角线
        | cv2.FLOODFILL_MASK_ONLY      # 只写 mask，不改原图
        | (255 << 8)                   # 填进 mask 的值
        | cv2.FLOODFILL_FIXED_RANGE
    )
    for tol in tolerances:
        mask.fill(0)
        try:
            # 第 4 个返回值就是填充区域的外接矩形，直接拿来用。
            # 自己去 mask 里扫一遍非零像素要慢十几倍。
            _, _, _, rect = cv2.floodFill(
                roi, mask, seed, 0, (tol,) * 3, (tol,) * 3, flags
            )
        except cv2.error as e:
            log.debug("颜色漫延失败（容差 %s）：%s", tol, e)
            continue

        left, top, cw, ch = rect
        right, bottom = left + cw - 1, top + ch - 1
        if cw < min_side or ch < min_side:
            continue
        # 贴到搜索窗口的边 = 这块区域比窗口还大，或者干脆是背景。
        # 这种结果没有意义，丢掉。
        touches_edge = (
            (left == 0 and x0 > 0) or (top == 0 and y0 > 0)
            or (right == rw - 1 and x1 < w) or (bottom == rh - 1 and y1 < h)
        )
        if touches_edge:
            continue
        out.append(QRect(x0 + left, y0 + top, cw, ch))
    return out


def _scan_levels(edges, x: int, y: int, strengths, min_side: int) -> list:
    """
    投影法：从 (x, y) 出发向四个方向找边界，一次算出多个强度下的结果。

    参数：
        edges     —— 边缘图（0/255）
        x, y      —— 起点（本屏物理坐标）
        strengths —— 若干个「边界线」判定强度（0~1）。
                     调大 -> 只认很强的边界 -> 框更大；调小 -> 框更小。
                     多个强度天然形成由小到大的层级。
        min_side  —— 小于这个边长的结果作废

    返回：矩形列表（可能为空）。

    为什么需要它：轮廓法要求边框是**闭合**的。图的边框缺了一角、
    或者被别的元素压住时，轮廓法什么都给不出来，投影法照样能找到四条边。

    为什么几个强度合在一个函数里算：行列求和是整个查询里最贵的一步
    （每次要扫过 400 万个像素）。每个强度各算一遍的话，鼠标一移动就发涩。
    求和结果和强度无关，算一次给所有强度共用。
    """
    import numpy as np

    h, w = edges.shape
    if not (0 <= x < w and 0 <= y < h):
        return []

    cache: dict[int, tuple] = {}

    def profiles(band: int):
        """算出「每一列/每一行在采样带内的边缘密度」。按采样带宽缓存。"""
        if band not in cache:
            y0, y1 = max(0, y - band), min(h, y + band + 1)
            x0, x1 = max(0, x - band), min(w, x + band + 1)
            col = edges[y0:y1, :].mean(axis=0) / 255.0
            row = edges[:, x0:x1].mean(axis=1) / 255.0
            cache[band] = (col, row)
        return cache[band]

    def bounds(col, row, strength: float):
        right_hits = np.nonzero(col[x + 1:] >= strength)[0]
        left_hits = np.nonzero(col[:x] >= strength)[0]
        bottom_hits = np.nonzero(row[y + 1:] >= strength)[0]
        top_hits = np.nonzero(row[:y] >= strength)[0]

        right = int(x + 1 + right_hits[0]) if right_hits.size else w - 1
        left = int(left_hits[-1]) if left_hits.size else 0
        bottom = int(y + 1 + bottom_hits[0]) if bottom_hits.size else h - 1
        top = int(top_hits[-1]) if top_hits.size else 0

        cw, ch = right - left + 1, bottom - top + 1
        if cw < min_side or ch < min_side:
            return None
        return QRect(left, top, cw, ch)

    out = []
    for strength in strengths:
        # 第一轮用固定采样带宽粗定位，第二轮用粗定位的尺寸当带宽再算一次。
        # 只用固定带宽的话，大图会因为采样带太窄，把图片内部的横线误当成边界。
        band = 48
        rect = None
        for _ in range(2):
            col, row = profiles(band)
            rect = bounds(col, row, strength)
            if rect is None:
                break
            new_band = max(16, min(rect.height(), rect.width()) // 2)
            if abs(new_band - band) < 8:
                break
            band = new_band
        if rect is not None:
            out.append(rect)
    return out


def candidates_at(
    index: RegionIndex, x: int, y: int, params: DetectParams
) -> list[QRect]:
    """
    找出所有包住 (x, y) 这个点的候选区域，按面积从小到大排成层级。

    参数：
        index  —— build_region_index() 的结果
        x, y   —— 鼠标位置（本屏物理坐标）
        params —— 识别参数

    返回：
        矩形列表，第 0 个是最贴合鼠标的小块，越往后越大。
        一个都找不到时返回空列表（界面上会提示可以手动拖框）。

    鼠标每移动一下就会调一次，所以这里只做查表和几次 numpy 求和，
    不做任何图像处理。
    """
    if not index.ready:
        return []

    min_side = params.min_side
    max_area = index.width * index.height * params.max_area_ratio
    pool: list[QRect] = []

    # 来源一：轮廓法。包住鼠标的都要。
    for r in index.rects:
        if r.contains(x, y):
            pool.append(r)

    # 来源二：投影法。几个强度各扫一次，天然形成由小到大的层级。
    for r in _scan_levels(index.edges, x, y, (0.60, 0.35, 0.20, 0.10), min_side):
        if r.width() * r.height() <= max_area:
            pool.append(r)

    # 来源三：颜色漫延法。专治 Canny 检不出的低对比度块。
    if index.bgr is not None:
        for r in _flood_bounds(index.bgr, x, y, (10, 24, 44), min_side):
            if r.width() * r.height() <= max_area:
                pool.append(r)

    if not pool:
        return []

    # 按面积从小到大。面积相同的按左上角排，保证结果稳定
    # （不稳定的话鼠标不动、框却在两个候选之间跳，看着像闪烁）。
    pool.sort(key=lambda r: (r.width() * r.height(), r.x(), r.y()))

    # 分层：面积差得太少的算同一层，只留第一个。
    # 不做这一步的话，滚轮要连滚五六下框才有肉眼可见的变化。
    levels: list[QRect] = []
    for r in pool:
        area = r.width() * r.height()
        if not levels:
            levels.append(r)
            continue
        last_area = levels[-1].width() * levels[-1].height()
        if area >= last_area * (1.0 + params.level_gap):
            levels.append(r)

    return levels
