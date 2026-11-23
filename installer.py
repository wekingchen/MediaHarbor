#!/usr/bin/env python3
"""Install a per-user launchd StartOnMount agent without administrator rights."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

LABEL = "io.local.mediaautoarchive"
HOME = Path.home()
APP_DIR = HOME / "Library/Application Support/MediaAutoArchive"
AGENTS = HOME / "Library/LaunchAgents"
PLIST = AGENTS / f"{LABEL}.plist"
SOURCE = Path(__file__).resolve().parent / "media_archive.py"


def agent_plist(python_path: Path, script_path: Path, app_dir: Path):
    """Pure launchd configuration generator; validated by CI on macOS."""
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python_path), str(script_path), "scan"],
        "RunAtLoad": True,
        "StartOnMount": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(app_dir / "agent-error.log"),
    }


def launchctl(*args):
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True)


def uninstall():
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{LABEL}")
    if PLIST.exists():
        launchctl("bootout", domain, str(PLIST))
        PLIST.unlink()
    print("已停止自动导入并卸载 launchd 服务。")
    print("你的图片、视频和数据库保留不动。要恢复使用可重新运行安装脚本。")


def install(target):
    if sys.platform != "darwin":
        raise RuntimeError("自动安装仅适用于 macOS。单元测试可在其他平台运行。")
    if sys.version_info < (3, 9):
        raise RuntimeError("需要 Python 3.9 或更新版本。请先安装 Python 3，然后重新运行。")
    if not SOURCE.exists():
        raise RuntimeError("压缩包中的 media_archive.py 不存在。")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    installed = APP_DIR / "media_archive.py"
    shutil.copy2(SOURCE, installed)
    # Keep the management menu and the uninstall command available even if
    # the original downloaded ZIP folder is later deleted.
    for extra in ("installer.py", "管理设置.command"):
        origin = SOURCE.parent / extra
        if origin.exists():
            shutil.copy2(origin, APP_DIR / extra)
    set_command = [sys.executable, str(installed), "configure"]
    if target:
        set_command += ["--target", target]
    print("\n第 1 步：选择归档目录（磁盘应已连接）")
    if subprocess.call(set_command) != 0:
        raise RuntimeError("目录设置未完成；旧的自动导入配置不变。")

    print("\n第 2 步：安装自动插卡触发器")
    content = agent_plist(Path(sys.executable), installed, APP_DIR)
    # Create log file first; launchd can fail if the parent directory is missing.
    (APP_DIR / "agent-error.log").touch(exist_ok=True)
    temp_plist = PLIST.with_suffix(".plist.tmp")
    with temp_plist.open("wb") as f:
        plistlib.dump(content, f, fmt=plistlib.FMT_XML, sort_keys=True)
    temp_plist.replace(PLIST)
    os.chmod(PLIST, 0o644)
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{LABEL}")
    result = launchctl("bootstrap", domain, str(PLIST))
    if result.returncode:
        raise RuntimeError(f"launchctl bootstrap 失败：{result.stderr.strip()}\n请检查登录会话或重新运行安装脚本。")
    print("\n安装完成！插入可移动 SD 卡 / U 盘后将自动扫描。")
    print('管理菜单：bash "$HOME/Library/Application Support/MediaAutoArchive/管理设置.command"')
    print("查看状态：", sys.executable, installed, "status")
    print("更改归档目录：", sys.executable, installed, "configure")
    print("手动扫描：", sys.executable, installed, "scan")
    print("日志路径：", APP_DIR / "activity.log")
    print("如果插入 U 盘没有触发，请通过管理菜单把磁盘名称加入白名单。")


def main():
    parser = argparse.ArgumentParser(description="媒体自动归档安装 / 卸载")
    parser.add_argument("--target", help="指定已经存在的保存文件夹（适合远程命令行安装）")
    parser.add_argument("--uninstall", action="store_true", help="停止和卸载自动导入，不删除文件及记录")
    args = parser.parse_args()
    try:
        if args.uninstall:
            uninstall()
        else:
            install(args.target)
    except (RuntimeError, OSError) as e:
        print("安装/卸载失败：", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())