"""
环境自检。装完依赖后先跑这个，全绿了再去跑 main.py。

用法（在项目根目录下）：
    python tools/env_check.py

它会挨个检查：Python 版本、依赖包、DPI 感知模式、显示器坐标、截图能力、
D 盘写入权限、全局热键注册、Chrome 位置（阶段 2 用）。

任何一项是 [失败]，先解决它再往下走——尤其是 DPI 那一项，
它不对的话框选位置一定会偏。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能 import 到项目根目录下的模块
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 尽量让中文在各种终端里都能正常显示
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 必须最早设置 DPI 感知，理由见 main.py 顶部的说明
from screen import geometry  # noqa: E402

_DPI_SET_RESULT = geometry.set_per_monitor_v2_awareness()

OK = "[  通过  ]"
BAD = "[  失败  ]"
WARN = "[  注意  ]"
INFO = "[  信息  ]"

_failures = 0
_warnings = 0


def report(status: str, title: str, detail: str = "") -> None:
    global _failures, _warnings
    if status is BAD:
        _failures += 1
    elif status is WARN:
        _warnings += 1
    print(f"{status} {title}")
    if detail:
        for line in str(detail).splitlines():
            print(f"           {line}")


def section(name: str) -> None:
    print()
    print(f"--- {name} " + "-" * max(0, 58 - len(name)))


# ---------------------------------------------------------------------------


def check_python() -> None:
    section("1. Python")
    v = sys.version_info
    text = f"{v.major}.{v.minor}.{v.micro}  ({sys.executable})"
    if (v.major, v.minor) >= (3, 11):
        report(OK, f"Python 版本 {v.major}.{v.minor}.{v.micro}", sys.executable)
    else:
        report(BAD, f"Python 版本太低：{text}", "本项目要求 3.11 或更高")

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv:
        report(OK, "正在虚拟环境中运行", sys.prefix)
    else:
        report(
            WARN,
            "没有使用虚拟环境",
            "不影响功能，但建议用 python -m venv .venv 隔离依赖",
        )


def check_packages() -> None:
    section("2. 依赖包")
    # (import 名, pip 包名, 阶段说明, 阶段 1 是否必需)
    packages = [
        ("PySide6", "PySide6", "界面", True),
        ("mss", "mss", "截图", True),
        ("pynput", "pynput", "全局热键", True),
        ("PIL", "Pillow", "图像处理", True),
        ("playwright", "playwright", "浏览器模式", True),
        ("httpx", "httpx", "下载兜底", True),
        ("cv2", "opencv-python", "自动识别（阶段 3）", False),
    ]
    for module_name, pip_name, purpose, required in packages:
        try:
            mod = __import__(module_name)
            version = getattr(mod, "__version__", "")
            if not version and module_name == "PySide6":
                from PySide6 import __version__ as v
                version = v
            report(OK, f"{pip_name} {version}".strip(), purpose)
        except Exception as e:
            if required:
                report(BAD, f"{pip_name} 不可用（{purpose}）", f"{e}\n解决：pip install -r requirements.txt")
            else:
                report(WARN, f"{pip_name} 不可用（{purpose}）", "现在还用不到，到那个阶段再装")


def check_dpi() -> None:
    section("3. DPI 感知模式（最关键的一项）")
    report(INFO, "设置结果", _DPI_SET_RESULT)
    actual = geometry.describe_awareness()
    if geometry.awareness_is_per_monitor():
        report(OK, "实际生效的模式", actual)
    else:
        report(
            BAD,
            "实际生效的模式不对",
            f"{actual}\n"
            "框选坐标一定会偏。通常是因为有别的库抢先设置了 DPI 模式。\n"
            "如果是直接跑这个脚本出现的，请把这行发给开发者。",
        )


def check_screens() -> None:
    section("4. 显示器与坐标换算")
    monitors = geometry.enum_display_monitors()
    if not monitors:
        report(BAD, "枚举不到任何显示器", "Win32 EnumDisplayMonitors 返回空")
        return
    for m in monitors:
        report(INFO, "物理显示器", str(m))
    vx, vy, vw, vh = geometry.virtual_desktop_rect()
    report(INFO, "虚拟桌面（物理像素）", f"{vw}x{vh} @({vx},{vy})")

    # 把 Qt 的视角也拉进来对照，这才是遮罩窗口真正依赖的东西
    try:
        from PySide6.QtGui import QGuiApplication
    except Exception as e:
        report(WARN, "跳过 Qt 屏幕对照", f"PySide6 不可用：{e}")
        return

    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    maps = geometry.map_screens(QGuiApplication.screens())
    for sm in maps:
        report(INFO, f"坐标映射 {sm.name}", sm.describe())
        if sm.match_method in ("index", "fallback-dpr"):
            report(
                WARN,
                f"{sm.name} 的配对方式是 {sm.match_method}",
                "没能按设备名或尺寸精确配对，坐标可能有误差，请留意框选结果",
            )
        # 自检换算公式：整屏应该正好映射回该屏的物理矩形
        x0, y0 = sm.to_physical(0, 0)
        x1, y1 = sm.to_physical(sm.lw, sm.lh)
        got = (x0, y0, x1 - x0, y1 - y0)
        want = (sm.px, sm.py, sm.pw, sm.ph)
        if got == want:
            report(OK, f"{sm.name} 整屏换算自检", f"逻辑 {sm.lw}x{sm.lh} -> 物理 {sm.pw}x{sm.ph}")
        else:
            report(BAD, f"{sm.name} 整屏换算自检不通过", f"算出 {got}，应该是 {want}")
    del app


def check_capture() -> None:
    section("5. 截图能力")
    try:
        from screen import capture
        image, virt = capture.grab_virtual_desktop()
    except Exception as e:
        report(BAD, "截图失败", str(e))
        return

    vw, vh = virt[2], virt[3]
    if (image.width(), image.height()) == (vw, vh):
        report(OK, "截图尺寸与虚拟桌面一致", f"{image.width()}x{image.height()}")
    else:
        report(
            BAD,
            "截图尺寸和虚拟桌面对不上",
            f"截到 {image.width()}x{image.height()}，虚拟桌面是 {vw}x{vh}。"
            "这通常意味着 DPI 感知模式不对。",
        )

    # 全黑的图基本可以断定截图失败了（锁屏、受保护内容）
    try:
        c = image.pixelColor(image.width() // 2, image.height() // 2)
        if (c.red(), c.green(), c.blue()) == (0, 0, 0):
            report(WARN, "画面中心是纯黑", "可能是屏幕被保护内容遮挡，或者你的桌面本来就是黑的")
        else:
            report(OK, "画面有实际内容", f"中心像素 RGB=({c.red()},{c.green()},{c.blue()})")
    except Exception as e:
        report(WARN, "读取像素失败", str(e))


def check_save_dir() -> None:
    section("6. 保存目录写入权限")
    try:
        from config import CONFIG
        from core import naming
    except Exception as e:
        report(BAD, "读取配置失败", str(e))
        return

    root = CONFIG.save_root()
    report(INFO, "保存根目录", str(root))
    target = naming.build_save_dir(
        root, "screen",
        screen_subdir=str(CONFIG.get("save.screen_subdir", "Screen")),
        split_by_date=bool(CONFIG.get("save.split_by_date", True)),
    )
    try:
        naming.ensure_dir(target)
    except OSError as e:
        report(BAD, "目录创建失败", str(e))
        return

    probe = target / "_envcheck_write_test.tmp"
    try:
        probe.write_bytes(b"mediagrabber write test")
        probe.unlink()
        report(OK, "今天的保存目录可读写", str(target))
    except OSError as e:
        report(BAD, "目录不可写", f"{target}\n{e}")


def check_hotkey() -> None:
    section("7. 全局热键")
    try:
        from config import CONFIG
        from pynput import keyboard
    except Exception as e:
        report(BAD, "pynput 不可用", str(e))
        return

    combo = str(CONFIG.get("hotkey.capture", "<ctrl>+<alt>+s"))
    try:
        listener = keyboard.GlobalHotKeys({combo: lambda: None})
        listener.daemon = True
        listener.start()
        listener.stop()
        report(OK, f"热键 {combo} 可以注册", "注意：pynput 无法检测热键是否被别的软件占用")
    except ValueError as e:
        report(
            BAD,
            f"热键字符串 {combo} 格式不对",
            f"{e}\n修饰键要写成 <ctrl> <alt> <shift>，例如 <ctrl>+<alt>+s",
        )
    except Exception as e:
        report(BAD, "热键注册失败", str(e))


def check_chrome() -> None:
    section("8. Chrome 与网络代理（浏览器模式用）")
    candidates: list[Path] = []
    try:
        import winreg
        for root, sub in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        ):
            try:
                with winreg.OpenKey(root, sub) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                    if value:
                        candidates.append(Path(value))
            except OSError:
                continue
    except ImportError:
        pass

    import os
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")

    for p in candidates:
        if p.exists():
            report(OK, "找到 Chrome", str(p))
            break
    else:
        report(WARN, "没找到 Chrome", "浏览器模式需要它。通用模式（框选）不受影响。")

    # 代理会不会劫持本机地址 —— 这是浏览器模式最隐蔽的一个坑
    import os as _os

    proxies = {
        k: v for k, v in _os.environ.items()
        if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    }
    if proxies:
        detail = "\n".join(f"{k}={v}" for k, v in proxies.items())
        no_proxy = _os.environ.get("NO_PROXY", "") + "," + _os.environ.get("no_proxy", "")
        if "127.0.0.1" in no_proxy or "localhost" in no_proxy:
            report(OK, "检测到代理，且已排除本机地址", detail)
        else:
            report(
                INFO,
                "检测到系统代理（程序已自动处理，不用管）",
                f"{detail}\n"
                "本程序访问 127.0.0.1 时会强制不走代理，所以不受影响。\n"
                "（写在这里只是让你知道有这么回事——如果哪天连不上浏览器，这是首要怀疑对象）",
            )


def main() -> int:
    print("=" * 70)
    print("  MediaGrabber 环境自检")
    print("=" * 70)

    check_python()
    check_packages()
    check_dpi()
    check_screens()
    check_capture()
    check_save_dir()
    check_hotkey()
    check_chrome()

    print()
    print("=" * 70)
    if _failures:
        print(f"  结果：{_failures} 项失败，{_warnings} 项注意。请先解决失败项。")
    elif _warnings:
        print(f"  结果：全部通过（有 {_warnings} 项注意事项，不影响当前功能）。")
    else:
        print("  结果：全部通过。双击 2-启动程序.bat 就能用了。")
    print("=" * 70)
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
