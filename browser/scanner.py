"""
页面媒体提取。

核心是一段注入到页面里执行的 JS（SCAN_JS），它负责把页面上所有能拿到的
图片/视频地址抠出来。之所以要在页面内跑，是因为很多信息只有浏览器自己知道：
  - srcset 里到底选中了哪一档（img.currentSrc）
  - CSS 背景图的最终计算值（getComputedStyle）
  - 图片的真实尺寸（naturalWidth，而不是显示尺寸）

Python 侧只做过滤和去重，不解析 HTML。

难点 3（懒加载）的处理策略，按优先级：
  1. img.currentSrc —— 浏览器已经替我们解析完 srcset 的结果，最准
  2. srcset / data-srcset 手动解析，取描述符数值最大的那一档
  3. 判定当前地址是占位图时，回退到 data-src / data-original / data-lazy-src /
     data-actualsrc / data-echo
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# 媒体类型
KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_BACKGROUND = "background"
KIND_POSTER = "poster"

# 特殊标记，界面上要区别对待
NOTE_STREAM = "stream"      # 分片流媒体，本工具不支持
NOTE_DRM = "drm"            # DRM 保护，无法抓取
NOTE_LAZY = "lazy"          # 来自懒加载属性，尺寸多半未知


# ---------------------------------------------------------------------------
# 注入页面执行的 JS
# ---------------------------------------------------------------------------
SCAN_JS = r"""
() => {
  const MAX_ELEMENTS = 4000;     // 扫背景图时最多看这么多元素，防止超大页面卡死
  const MIN_BG_BOX = 50;         // 背景图元素小于这个尺寸就不看了
  const out = [];
  const seen = new Set();

  const abs = (u) => {
    if (!u) return "";
    u = String(u).trim();
    if (!u || u === "about:blank") return "";
    try { return new URL(u, document.baseURI).href; } catch (e) { return ""; }
  };

  // 判断一个地址是不是占位图 / 无意义的小图标。
  //
  // 关键词必须用分隔符卡住边界（前面是 / _ - 或开头，后面是 . _ - 或结尾），
  // 不能直接子串匹配：像 lazy.png、blanket.jpg、loading-dock.jpg 这种
  // 正常图片会被误杀。这类误杀非常隐蔽——图明明在页面上，扫描就是抓不到。
  const PLACEHOLDER_RE =
    /(?:^|[\/_-])(?:placeholder|blank|spacer|loading|dummy|transparent|1x1|pixel|noimage|no-image)(?:[._-]|$)/i;

  const isPlaceholder = (url, w, h) => {
    if (!url) return true;
    // 尺寸明确且足够大，就不可能是占位图，直接放行。
    // 占位图和 LQIP（模糊小图预览）的真实尺寸都很小，不会到这个量级。
    if (w >= 200 && h >= 200) return false;
    if (w > 0 && w <= 2 && h > 0 && h <= 2) return true;
    if (url.startsWith("data:") && url.length < 1000) return true;
    return PLACEHOLDER_RE.test(url);
  };

  // 解析 srcset，取描述符（800w 或 2x）数值最大的那一档。
  // 注意：srcset 里的 URL 理论上可以含逗号，这里用简单切分，
  // 极少数含逗号的 URL 会解析失败——但主路径走的是 currentSrc，不依赖这里。
  const bestFromSrcset = (srcset) => {
    if (!srcset) return "";
    let best = "", bestScore = -1;
    for (const raw of String(srcset).split(",")) {
      const part = raw.trim();
      if (!part) continue;
      const bits = part.split(/\s+/);
      const url = bits[0];
      if (!url) continue;
      let score = 1;
      const desc = bits[1] || "";
      const m = desc.match(/^([\d.]+)([wx])$/);
      if (m) score = parseFloat(m[1]) * (m[2] === "x" ? 1000 : 1);
      if (score > bestScore) { bestScore = score; best = url; }
    }
    return best;
  };

  // 把一个元素的 srcset 拆成 [{url, n, unit}] 列表
  const srcsetEntries = (el) => {
    const raw = (el && (el.getAttribute("srcset") || el.getAttribute("data-srcset"))) || "";
    const list = [];
    for (const part of String(raw).split(",")) {
      const bits = part.trim().split(/\s+/);
      if (!bits[0]) continue;
      const m = (bits[1] || "").match(/^([\d.]+)([wx])$/);
      list.push({ url: abs(bits[0]), n: m ? parseFloat(m[1]) : 0, unit: m ? m[2] : "" });
    }
    return list;
  };

  // 还原 srcset 图片的**真实文件尺寸**。
  //
  // 坑：按 HTML 规范，从 srcset 的 w 描述符里选中的图，浏览器报的
  // naturalWidth 是 **文件真实宽度 ÷ 有效像素密度**，而不是文件真实宽度。
  //
  // 密度两个方向都可能偏，实测（同一张 1400x1000 的图）：
  //   放在 1036 CSS 像素宽的位置 -> 密度 1.35 -> naturalWidth 报 1036（缩小了）
  //   放在 1707 CSS 像素宽的位置 -> 密度 0.82 -> naturalWidth 报 1707（放大了）
  //
  // 所以既不能只往大了修正、也不能只往小了修正——w 描述符声明的就是文件
  // 真实宽度，命中就无条件采信它。
  // 宽度精确；高度按宽高比反算，因为 naturalHeight 本身已被取整，
  // 可能有 1~2 像素误差，只影响界面上显示的数字，无伤大雅。
  const realSize = (img, url, w, h) => {
    if (!w || !h) return [w, h];
    const sources = [img];
    const parent = img.parentElement;
    if (parent && parent.tagName === "PICTURE") {
      for (const s of parent.querySelectorAll("source")) sources.push(s);
    }
    for (const el of sources) {
      for (const e of srcsetEntries(el)) {
        if (e.unit === "w" && e.url === url && e.n > 0) {
          return [Math.round(e.n), Math.round(h * e.n / w)];
        }
      }
    }
    return [w, h];
  };

  const push = (url, opts) => {
    const u = abs(url);
    if (!u) return;
    if (u.startsWith("javascript:")) return;
    if (seen.has(u)) return;
    seen.add(u);
    out.push({
      url: u,
      width: opts.width || 0,
      height: opts.height || 0,
      tag: opts.tag || "",
      alt: (opts.alt || "").slice(0, 200),
      title: (opts.title || "").slice(0, 200),
      kind: opts.kind || "image",
      note: opts.note || "",
      referrer: location.href,
    });
  };

  // ---- 1. <img> --------------------------------------------------------
  for (const img of Array.from(document.images)) {
    // 优先用浏览器已经解析好的 currentSrc
    let url = img.currentSrc || img.src || "";
    let note = "";

    // naturalWidth 对 srcset 图片是密度换算过的，先还原成真实文件尺寸
    const [w, h] = realSize(img, url, img.naturalWidth || 0, img.naturalHeight || 0);

    // currentSrc 是占位图时，试着从 srcset 里挑一个更好的
    if (isPlaceholder(url, w, h)) {
      const fromSet = bestFromSrcset(img.getAttribute("srcset") || img.dataset.srcset);
      if (fromSet && !isPlaceholder(fromSet, 0, 0)) {
        url = fromSet;
        note = "lazy";
      }
    }

    // 还是占位图，就翻各家懒加载框架的自定义属性
    if (isPlaceholder(url, w, h)) {
      const attrs = [
        "data-src", "data-original", "data-lazy-src", "data-actualsrc",
        "data-echo", "data-url", "data-image", "data-hi-res-src",
      ];
      for (const a of attrs) {
        const v = img.getAttribute(a);
        if (v && !isPlaceholder(v, 0, 0)) { url = v; note = "lazy"; break; }
      }
    }

    push(url, {
      width: note === "lazy" ? 0 : w,     // 懒加载属性还没加载，尺寸不可信
      height: note === "lazy" ? 0 : h,
      tag: "img",
      alt: img.alt,
      title: img.title,
      kind: "image",
      note: note,
    });
  }

  // ---- 2. <picture> 里的 <source> --------------------------------------
  for (const src of Array.from(document.querySelectorAll("picture source"))) {
    const best = bestFromSrcset(src.getAttribute("srcset") || src.dataset.srcset);
    if (best) push(best, { tag: "source", kind: "image", note: "lazy" });
  }

  // ---- 3. <video> ------------------------------------------------------
  for (const v of Array.from(document.querySelectorAll("video"))) {
    const w = v.videoWidth || 0;
    const h = v.videoHeight || 0;
    const url = v.currentSrc || v.src || "";
    let note = "";
    // DRM 判定：挂了 mediaKeys 就是加密内容，抓不了
    try { if (v.mediaKeys) note = "drm"; } catch (e) {}
    // 分片流判定：blob: 地址通常来自 MediaSource，是一堆 .ts 片段拼的
    if (!note && url.startsWith("blob:")) note = "stream";

    if (url) {
      push(url, {
        width: w, height: h, tag: "video",
        title: v.title, kind: "video", note: note,
      });
    }
    // 子 <source>
    for (const s of Array.from(v.querySelectorAll("source"))) {
      const su = s.getAttribute("src");
      if (su) push(su, { width: w, height: h, tag: "source", kind: "video", note: note });
    }
    // 封面图也一并收了，经常是高清大图
    if (v.poster) push(v.poster, { tag: "video", kind: "poster", title: v.title });
  }

  // ---- 4. CSS 背景图 ---------------------------------------------------
  const all = document.querySelectorAll("*");
  const limit = Math.min(all.length, MAX_ELEMENTS);
  for (let i = 0; i < limit; i++) {
    const el = all[i];
    let rect;
    try { rect = el.getBoundingClientRect(); } catch (e) { continue; }
    if (rect.width < MIN_BG_BOX || rect.height < MIN_BG_BOX) continue;
    let bg;
    try { bg = getComputedStyle(el).backgroundImage; } catch (e) { continue; }
    if (!bg || bg === "none") continue;
    // 一个元素可能有多层背景，逐个抠 url(...)
    const re = /url\((['"]?)(.*?)\1\)/g;
    let m;
    while ((m = re.exec(bg)) !== null) {
      const u = m[2];
      // CSS 背景里的 SVG 基本都是网站自己的界面图标、按钮、装饰图，
      // 不是你想抓的内容；而且这类静态资源往往禁止直接访问（403）。
      // 内容图几乎不会以 SVG 形式出现在 CSS 背景里，所以直接跳过。
      const isSvg = /^data:image\/svg/i.test(u) || /\.svg(\?|#|$)/i.test(u);
      if (u && !isSvg) {
        // 尺寸填 0（未知），**不能填元素框的尺寸**：
        // background-size:cover 时元素框和图片真实尺寸毫无关系，
        // 一张 1000x800 的图放在 400x300 的盒子里会被报成 400x300，
        // 尺寸滑块就会把它误筛掉。真实尺寸等缩略图加载出来再回填。
        push(u, {
          width: 0,
          height: 0,
          tag: el.tagName.toLowerCase(),
          kind: "background",
        });
      }
    }
  }

  return out;
}
"""


def filter_items(
    items: list[dict[str, Any]],
    *,
    min_width: int = 200,
    min_height: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    过滤扫描结果，并按 URL 去重。

    参数：
        items      —— 各个 frame 扫出来的原始条目拼在一起
        min_width  —— 最小宽度，小于它的丢掉
        min_height —— 最小高度

    返回：(保留下来的条目, 各类丢弃原因的计数)

    一个重要的判断：**尺寸未知的条目一律保留**。
    懒加载的图在扫描时还没真正加载，naturalWidth 是 0；如果按「小于 200 就丢」
    的规则处理，恰恰会把我们费劲提取出来的懒加载图全部丢光。所以只丢弃
    「已知尺寸且确实太小」的，尺寸未知的留给用户自己判断。
    """
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    stats = {"重复": 0, "尺寸过小": 0, "无效地址": 0, "小图标": 0}

    for item in items:
        url = str(item.get("url") or "").strip()
        if not url:
            stats["无效地址"] += 1
            continue
        if url in seen:
            stats["重复"] += 1
            continue

        w = int(item.get("width") or 0)
        h = int(item.get("height") or 0)
        size_known = w > 0 and h > 0

        # base64 小图标：不到 3KB 的 data: 地址基本都是图标或占位符。
        # 但这只是**尺寸未知时的兜底猜测**——图片压缩率差异极大，
        # 一张 300x300 的纯色 PNG 编码出来还不到 2KB，按长度猜就会误杀。
        # 尺寸已经拿到了就走下面统一的尺寸判断，不用猜。
        if url.startswith("data:") and not size_known and len(url) < 3000:
            stats["小图标"] += 1
            continue

        # 只有在尺寸确实拿到了的情况下才按尺寸筛
        if size_known and (w < min_width or h < min_height):
            stats["尺寸过小"] += 1
            continue

        seen.add(url)
        kept.append(item)

    return kept, stats


def describe_item(item: dict[str, Any]) -> str:
    """给缩略图网格用的一行说明：尺寸 + 格式。"""
    w = int(item.get("width") or 0)
    h = int(item.get("height") or 0)
    size = f"{w}x{h}" if w and h else "尺寸未知"
    ext = guess_extension(str(item.get("url") or "")).lstrip(".").upper() or "?"
    return f"{size}  {ext}"


def guess_extension(url: str, content_type: str = "") -> str:
    """
    猜文件扩展名。先看 Content-Type，再看 URL 结尾，都不行就默认 .jpg。

    什么情况会不准：CDN 经常给没有扩展名的 URL（例如 /image/abc123），
    这时只能靠 Content-Type；两者都没有时退回 .jpg，虽然可能不对，
    但至少能存下来、双击也能打开（Windows 会按内容识别）。
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    ct_map = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/gif": ".gif", "image/webp": ".webp", "image/avif": ".avif",
        "image/bmp": ".bmp", "image/svg+xml": ".svg", "image/tiff": ".tif",
        "image/x-icon": ".ico",
        "video/mp4": ".mp4", "video/webm": ".webm", "video/ogg": ".ogv",
        "video/quicktime": ".mov", "video/x-matroska": ".mkv",
    }
    if ct in ct_map:
        return ct_map[ct]

    known = (
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg",
        ".tif", ".tiff", ".ico", ".mp4", ".webm", ".mov", ".mkv", ".ogv",
    )
    # 去掉查询串和锚点再看结尾
    path = url.split("#")[0].split("?")[0].lower()
    for ext in known:
        if path.endswith(ext):
            return ".jpeg" if ext == ".jpeg" else ext

    if url.startswith("data:"):
        head = url[5:60].split(";")[0]
        if head in ct_map:
            return ct_map[head]

    return ".jpg"
