"""
打包成 exe（阶段 4）。

用法（在项目根目录）：

    .venv\\Scripts\\python.exe build.py            # 单文件，双击即用（默认）
    .venv\\Scripts\\python.exe build.py --onedir   # 单目录，启动快，便于排查问题

做完之后：
    单文件模式 -> dist\\MediaGrabber.exe             这一个文件就是全部
    单目录模式 -> dist\\MediaGrabber\\MediaGrabber.exe  整个文件夹要一起拷

打包要花几分钟，中间会刷很多行输出，那是正常的。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC = ROOT / "mediagrabber.spec"


def human_size(num_bytes: float) -> str:
    """把字节数写成人看的样子。"""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def dir_size(path: Path) -> int:
    """一个目录里所有文件加起来多大。"""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def check_prerequisites() -> list[str]:
    """
    打包前先确认该有的都有。

    返回：问题清单，空列表表示一切正常。

    在这儿拦一道，是因为 PyInstaller 缺东西时的报错又长又难懂，
    不如提前用人话说清楚。
    """
    problems = []

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        problems.append(
            "没装 PyInstaller。执行：.venv\\Scripts\\python.exe -m pip install pyinstaller"
        )

    try:
        import playwright
        driver = Path(playwright.__file__).parent / "driver"
        if not driver.exists():
            problems.append(
                f"playwright 的 driver 目录不存在（{driver}）。\n"
                f"   浏览器模式会打不开。执行：.venv\\Scripts\\python.exe -m pip install --force-reinstall playwright"
            )
    except ImportError:
        problems.append("没装 playwright，浏览器模式会不可用。执行：pip install -r requirements.txt")

    for mod, name in (("cv2", "opencv-python"), ("PySide6", "PySide6-Essentials"),
                      ("mss", "mss"), ("pynput", "pynput"), ("PIL", "Pillow")):
        try:
            __import__(mod)
        except ImportError:
            problems.append(f"没装 {name}。执行：pip install -r requirements.txt")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="把 MediaGrabber 打包成 exe")
    parser.add_argument(
        "--onedir", action="store_true",
        help="打包成一个文件夹（启动快，便于排查问题），默认是单文件",
    )
    parser.add_argument(
        "--keep-build", action="store_true",
        help="保留 build 中间目录（默认打包完就删掉）",
    )
    args = parser.parse_args()
    onefile = not args.onedir

    print("=" * 70)
    print(f"  打包 MediaGrabber（{'单文件' if onefile else '单目录'}模式）")
    print("=" * 70)

    problems = check_prerequisites()
    if problems:
        print("\n打包没法开始，先解决这些：\n")
        for p in problems:
            print(f" - {p}")
        return 1

    # --- 图标 ---------------------------------------------------------------
    print("\n[1/4] 生成图标…")
    try:
        from tools.make_icon import build_icon

        icon = build_icon()
        print(f"      {icon}")
    except Exception as e:
        # 图标不是必需的，没有就用 PyInstaller 的默认图标，不该因此中断打包
        print(f"      生成失败（{e}），这次用默认图标，不影响功能")

    # --- 清理旧产物 ---------------------------------------------------------
    print("\n[2/4] 清理上次的产物…")
    for path in (DIST, BUILD):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            print(f"      删掉了 {path.name}/")

    # --- 打包 ---------------------------------------------------------------
    print("\n[3/4] 调用 PyInstaller（要几分钟，中间刷屏是正常的）…\n")
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["MG_ONEFILE"] = "1" if onefile else "0"

    t0 = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)],
        cwd=str(ROOT),
        env=env,
    )
    elapsed = time.perf_counter() - t0

    if result.returncode != 0:
        print(f"\nPyInstaller 失败了（返回码 {result.returncode}）。")
        print("把上面最后那几十行输出发给我。")
        return result.returncode

    # --- 检查产物 -----------------------------------------------------------
    print(f"\n[4/4] 检查产物（打包用时 {elapsed / 60:.1f} 分钟）…")

    if onefile:
        target = DIST / "MediaGrabber.exe"
        if not target.exists():
            print(f"      奇怪，没找到 {target}")
            return 1
        size = target.stat().st_size
        print(f"      {target}")
        print(f"      大小：{human_size(size)}")
    else:
        target = DIST / "MediaGrabber" / "MediaGrabber.exe"
        if not target.exists():
            print(f"      奇怪，没找到 {target}")
            return 1
        size = dir_size(DIST / "MediaGrabber")
        print(f"      {target}")
        print(f"      整个文件夹：{human_size(size)}")

        # 单目录模式下能直接确认 driver 在不在，这是最容易漏的东西
        driver = DIST / "MediaGrabber" / "_internal" / "playwright" / "driver"
        if driver.exists():
            print(f"      playwright driver：在（{human_size(dir_size(driver))}）")
        else:
            print("      playwright driver：**没找到**，浏览器模式会用不了")

    if not args.keep_build and BUILD.exists():
        shutil.rmtree(BUILD, ignore_errors=True)
        print("      清掉了 build 中间目录")

    print("\n" + "=" * 70)
    print("  打包完成")
    print("=" * 70)
    print(f"\n双击运行：{target}")
    print("\n第一次启动会比较慢（要解压内容），之后快一些。")
    print("config.json、logs、chrome-profile 都会生成在 exe 旁边。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
