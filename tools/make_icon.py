"""
生成 exe 用的图标文件 assets/mediagrabber.ico。

打包（build.py）会自动调它，正常情况下你不用手动运行。
想单独看看图标长什么样时可以执行：

    .venv\\Scripts\\python.exe tools\\make_icon.py

图标本身是用代码画的（见 ui/appicon.py），所以托盘图标和 exe 图标
永远是同一个东西，不会出现两处不一致。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 画图标要用 QPixmap，而 QPixmap 必须有 QGuiApplication 才能创建。
# 这里用 offscreen 平台，不会真的弹出任何窗口。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication  # noqa: E402

from ui.appicon import ICO_SIZES, write_ico  # noqa: E402

ICON_PATH = _ROOT / "assets" / "mediagrabber.ico"


def build_icon(path: Path = ICON_PATH) -> Path:
    """
    画出图标并写成 .ico。

    参数：path —— 输出路径，默认 assets/mediagrabber.ico
    返回：写好的文件路径

    什么情况会失败：
        - Pillow 没装   -> ImportError
        - 目录不可写     -> OSError
    """
    app = QGuiApplication.instance() or QGuiApplication([])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_ico(path)
    del app
    return path


if __name__ == "__main__":
    out = build_icon()
    size_kb = out.stat().st_size / 1024
    print(f"图标已生成：{out}")
    print(f"包含尺寸：{', '.join(f'{s}x{s}' for s in ICO_SIZES)}")
    print(f"文件大小：{size_kb:.1f} KB")
