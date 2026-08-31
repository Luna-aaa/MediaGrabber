# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（阶段 4）。

不要直接跑这个文件，用 build.py：

    .venv\\Scripts\\python.exe build.py

build.py 会先生成图标、清理旧产物，再调用它。

===========================================================================
 这里面每一段都是踩过坑才加上的，删之前先看注释
===========================================================================

1. **playwright 必须 collect_all。**
   playwright 不是纯 Python 包——它靠一个自带的 Node.js 进程干活
   （playwright/driver/ 目录，一百多 MB，里面是 node.exe 加一堆 js）。
   PyInstaller 只分析 import 语句，根本看不见这些文件。
   收不全的后果：程序能启动、框选也正常，只有点「启动浏览器」时报
   「Executable doesn't exist」——**一个只在打包后才出现的故障**。

2. **不打包 Chromium 浏览器。**
   本项目用 connect_over_cdp 连你自己装的 Chrome，不需要 playwright
   自带的浏览器。那玩意儿另外还有几百 MB。

3. **排除用不到的 Qt 模块。**
   PySide6-Essentials 装出来 207 MB，本项目只用 QtCore/QtGui/QtWidgets。
   QML、Quick、3D、多媒体那些一个都用不到，全排掉能省下一大块。
   排除列表是保守的——宁可多留几 MB，也不能因为漏了个间接依赖
   让程序在你机器上起不来。
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
ICON = ROOT / "assets" / "mediagrabber.ico"

# build.py 通过这个环境变量选择打包方式，默认单文件
ONEFILE = os.environ.get("MG_ONEFILE", "1") == "1"

# --- playwright：把 Node driver 整个收进来 ---------------------------------
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")

# --- 那些藏在函数里的 import，静态分析容易漏 -------------------------------
# 本项目故意把 cv2 / mss / numpy 放在函数内部 import（见各模块的说明），
# 这里显式声明一遍，确保它们一定被打进去。
hiddenimports = [
    "cv2",
    "numpy",
    "mss",
    "mss.windows",
    "PIL.Image",
    "pynput.keyboard",
    "pynput.keyboard._win32",
    "pynput.mouse",
    "pynput.mouse._win32",
    "playwright.async_api",
    "httpx",
] + pw_hidden

# --- 用不到的大块，排掉 ----------------------------------------------------
excludes = [
    # 本项目只用 QtCore / QtGui / QtWidgets
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebSockets", "PySide6.QtWebChannel", "PySide6.QtWebView",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtTest", "PySide6.QtHelp",
    "PySide6.QtSql", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    # 其它用不到的
    "tkinter", "unittest", "pydoc", "doctest",
    "matplotlib", "scipy", "pandas", "IPython", "notebook",
]

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=pw_binaries,
    datas=pw_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

if ONEFILE:
    # 单文件：双击一个 exe 就能用。代价是每次启动要把内容解压到临时目录，
    # 比单目录慢几秒。
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="MediaGrabber",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,          # UPX 压缩会被不少杀毒软件当成可疑行为，不值得
        upx_exclude=[],
        runtime_tmpdir=None,
        # console=False：不要那个黑窗口。
        # 注意这会让 sys.stdout / sys.stderr 变成 None——
        # core/logger.py 和 main.py 里都为此做了处理，别把那些判断删了。
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ICON) if ICON.exists() else None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="MediaGrabber",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ICON) if ICON.exists() else None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="MediaGrabber",
    )
