"""CourseAgent 安装向导：让用户自选安装位置。

用法（发布出去的 exe 内置此功能）：
    CourseAgent.exe --install          # 弹图形安装向导
    CourseAgent.exe --install "D:\\工具"  # 命令行静默装到指定目录
    CourseAgent.exe --uninstall        # 弹卸载确认，清理快捷方式

安装内容：
  - 把自身复制到 <目标>\\CourseAgent\\CourseAgent.exe
  - 生成 <目标>\\CourseAgent\\config.example.json（从资源里带出来）
  - 可选：桌面快捷方式、开始菜单快捷方式
  - 写 <目标>\\CourseAgent\\uninstall.exe 入口信息（复用本程序 --uninstall）

不写注册表、不写系统目录；卸载 = 删文件夹 + 删快捷方式，绿色软件。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import os
from pathlib import Path

APP_DIR_NAME = "CourseAgent"
EXE_NAME = "CourseAgent.exe"
LNK_DESKTOP = "CourseAgent.lnk"
LNK_STARTMENU = Path("Programs") / "CourseAgent" / LNK_DESKTOP
UNINSTALL_LNK_DESKTOP = "卸载 CourseAgent.lnk"
UNINSTALL_LNK_STARTMENU = Path("Programs") / "CourseAgent" / UNINSTALL_LNK_DESKTOP


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def self_exe() -> Path:
    """当前 exe 路径（打包后）；源码运行时指向 python + main.py，不适用安装器。"""
    if is_frozen():
        return Path(sys.executable).resolve()
    raise RuntimeError("安装向导仅对打包后的 exe 可用（源码运行请直接使用项目目录）")


def default_install_root() -> Path:
    """默认安装位置（用户可改）：返回**父目录**，安装时在其下建 CourseAgent 子目录。

    很多人 C 盘紧张，默认往 C 盘装会被吐槽；但也要避免挑到只剩几百 MB 的盘。
    规则：C 盘以外、剩余 > 5GB 的盘中选剩余最大的；没有就退到 C:\\。
    """
    import shutil as _sh

    candidates: list[tuple[int, Path]] = []
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{letter}:/")
        if not drive.is_dir():
            continue
        try:
            free = _sh.disk_usage(str(drive)).free
        except OSError:
            continue
        if free > 5 * 1024 ** 3:            # 至少 5GB 余量
            candidates.append((free, drive))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return Path("C:/")


def desktop_dir() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def start_menu_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu"


def _resource(name: str) -> Path | None:
    """单文件版资源解压目录里的文件。"""
    base = getattr(sys, "_MEIPASS", "")
    if base:
        p = Path(base) / name
        if p.is_file():
            return p
    return None


def install_to(target_root: Path, desktop: bool = True,
               start_menu: bool = True) -> Path:
    """把程序装到 target_root\\CourseAgent\\，返回安装目录。"""
    exe = self_exe()
    app_dir = target_root / APP_DIR_NAME
    app_dir.mkdir(parents=True, exist_ok=True)

    # 1) 主程序（自身复制）
    dest_exe = app_dir / EXE_NAME
    if exe.resolve() != dest_exe.resolve():
        shutil.copy2(exe, dest_exe)

    # 2) 示例配置（从资源解压目录带出来；用户改过的 config.json 不覆盖）
    example = _resource("config.example.json")
    if example:
        dest_cfg = app_dir / "config.example.json"
        if not dest_cfg.exists():
            shutil.copy2(example, dest_cfg)

    # 3) 快捷方式（PowerShell 生成 .lnk，免额外依赖）
    if desktop:
        _make_shortcut(desktop_dir() / LNK_DESKTOP, dest_exe, app_dir, "CourseAgent 直播课整理")
    if start_menu:
        sm = start_menu_dir() / LNK_STARTMENU.parent
        sm.mkdir(parents=True, exist_ok=True)
        _make_shortcut(sm / LNK_DESKTOP, dest_exe, app_dir, "CourseAgent 直播课整理")

    # 4) 卸载入口：写一个批处理（比再复制一份 exe 直观）
    # 4) 卸载入口：批处理删快捷方式，再自删目录。
    #    坑：目录路径含空格/中文时，嵌套引号会被 cmd 解析错。
    #    做法——批处理里先 cd 到自身目录，再用 %~dp0 以外的相对方式自删，
    #    并用 pushd/popd 规避盘符切换问题。
    un = app_dir / "uninstall.bat"
    un.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f"echo 即将卸载 {APP_DIR_NAME}\r\n"
        "choice /C YN /M \"确认卸载（你的 data/output/config.json 会一并删除）\"\r\n"
        "if errorlevel 2 exit /b 0\r\n"
        f"del /q \"{desktop_dir() / LNK_DESKTOP}\" 2>nul\r\n"
        f"rmdir /s /q \"{start_menu_dir() / LNK_STARTMENU.parent}\" 2>nul\r\n"
        "rem 自身目录：切到上级再删本目录，避免删不掉正在运行的文件\r\n"
        "pushd \"%~dp0..\"\r\n"
        f"start \"\" /min cmd /c \"timeout /t 2 >nul & rmdir /s /q \\\"%~dp0.\\\"\"\r\n"
        "popd\r\n",
        encoding="utf-8",
    )
    return app_dir


def _make_shortcut(lnk: Path, target: Path, workdir: Path, desc: str) -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{lnk}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.WorkingDirectory = '{workdir}'; "
        f"$s.Description = '{desc}'; "
        "$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, text=True, timeout=30)


def remove_install(dest_root: Path) -> list[str]:
    """卸载：删快捷方式与程序目录。返回说明列表。"""
    notes: list[str] = []
    for lnk in (desktop_dir() / LNK_DESKTOP,):
        if lnk.exists():
            lnk.unlink()
            notes.append(f"已删除 {lnk}")
    sm = start_menu_dir() / LNK_STARTMENU.parent
    if sm.is_dir():
        shutil.rmtree(sm, ignore_errors=True)
        notes.append(f"已删除 {sm}")
    app_dir = dest_root / APP_DIR_NAME
    if app_dir.is_dir():
        notes.append(f"程序目录 {app_dir} 请在关闭程序后手动删除"
                     f"（内含你的 data/、output/ 与 config.json）")
    return notes


# ------------------------------------------------------------------ GUI
def run_gui_install(preferred: Path | None = None) -> int:
    """tkinter 安装向导：选目录 → 安装 → 完成提示。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    exe = self_exe()
    default_root = preferred or default_install_root()

    root = tk.Tk()
    root.title("CourseAgent 安装向导")
    root.geometry("560x320")
    root.resizable(False, False)

    var_dir = tk.StringVar(value=str(default_root))
    var_desktop = tk.BooleanVar(value=True)
    var_start = tk.BooleanVar(value=True)

    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="CourseAgent 安装向导", font=("Microsoft YaHei", 14, "bold")
              ).pack(anchor="w", pady=(0, 4))
    ttk.Label(frm, text="选择安装位置（将在该目录下创建 CourseAgent 文件夹）",
              ).pack(anchor="w", pady=(0, 10))

    row = ttk.Frame(frm)
    row.pack(fill="x")
    ttk.Label(row, text="安装到：").pack(side="left")
    entry = ttk.Entry(row, textvariable=var_dir)
    entry.pack(side="left", fill="x", expand=True, padx=6)

    def browse():
        d = filedialog.askdirectory(initialdir=var_dir.get() or str(Path.home()),
                                    title="选择安装位置")
        if d:
            var_dir.set(d)

    ttk.Button(row, text="浏览…", command=browse).pack(side="left")

    ttk.Checkbutton(frm, text="创建桌面快捷方式", variable=var_desktop
                    ).pack(anchor="w", pady=(10, 0))
    ttk.Checkbutton(frm, text="创建开始菜单快捷方式", variable=var_start
                    ).pack(anchor="w")

    status = tk.StringVar(value="")
    ttk.Label(frm, textvariable=status, foreground="#555").pack(anchor="w", pady=(12, 0))

    def do_install():
        target = Path(var_dir.get().strip())
        if not target.drive or len(str(target)) < 3:
            messagebox.showerror("无效路径", "请选择一个有效的磁盘目录")
            return
        status.set("正在安装…")
        root.update()
        try:
            app_dir = install_to(target, desktop=var_desktop.get(),
                                 start_menu=var_start.get())
        except PermissionError as e:
            messagebox.showerror("权限不足",
                                 f"无法写入该目录（{e}）。\n"
                                 "请换一个目录（例如 D:\\CourseAgent），"
                                 "或以管理员身份运行。")
            status.set("")
            return
        except Exception as e:
            messagebox.showerror("安装失败", str(e))
            status.set("")
            return
        status.set("安装完成")
        launch = messagebox.askyesno(
            "安装完成",
            f"已安装到：\n{app_dir}\n\n现在启动 CourseAgent 吗？")
        if launch:
            subprocess.Popen([str(app_dir / EXE_NAME)], cwd=str(app_dir),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        root.destroy()

    ttk.Button(frm, text="安装", command=do_install).pack(anchor="e", pady=(12, 0))

    root.mainloop()
    return 0


def run_gui_uninstall() -> int:
    import tkinter as tk
    from tkinter import messagebox

    # 单文件版：exe 在 <root>\CourseAgent\CourseAgent.exe，dest_root 就是 <root>
    exe = self_exe()
    app_dir = exe.parent
    dest_root = app_dir.parent

    root = tk.Tk()
    root.withdraw()
    if not messagebox.askyesno(
            "卸载 CourseAgent",
            f"将删除桌面/开始菜单快捷方式。\n\n"
            f"程序目录：{app_dir}\n"
            f"（含你的 data/、output/、config.json，不会被自动删除）\n\n"
            f"继续吗？"):
        root.destroy()
        return 0
    notes = remove_install(dest_root)          # 只调一次
    root.destroy()
    print("\n".join(notes))
    return 0


def remove_install_notes(dest_root: Path) -> list[str]:
    return [f"程序目录：{dest_root / APP_DIR_NAME}"
            f"（含你的 data/ output/ config.json，请手动确认后删除）"]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="CourseAgent 安装/卸载向导")
    ap.add_argument("--install", nargs="?", const="__GUI__", default=None,
                    metavar="DIR", help="安装到指定目录；不带值则弹图形向导")
    ap.add_argument("--uninstall", action="store_true", help="卸载")
    ap.add_argument("--silent", action="store_true",
                    help="配合 --install DIR 使用：静默安装，不弹窗（脚本/自动化用）")
    ap.add_argument("--no-shortcuts", action="store_true",
                    help="静默安装时不创建快捷方式")
    args, _rest = ap.parse_known_args(argv[1:])

    if args.install is not None:
        if args.install == "__GUI__":
            return run_gui_install()
        # 指定了目录：默认仍走 GUI（让用户确认位置），--silent 则直接装
        if args.silent:
            return run_silent_install(
                Path(args.install),
                shortcuts=not args.no_shortcuts)
        return run_gui_install(Path(args.install))
    if args.uninstall:
        return run_gui_uninstall()
    ap.print_help()
    return 1


def run_silent_install(target_root: Path, shortcuts: bool = True) -> int:
    """命令行静默安装（不弹窗），供脚本或自动化使用。"""
    try:
        app_dir = install_to(target_root, desktop=shortcuts,
                             start_menu=shortcuts)
    except PermissionError as e:
        print(f"安装失败：权限不足，无法写入 {target_root}（{e}）")
        print("请换一个目录，或以管理员身份运行。")
        return 2
    except Exception as e:
        print(f"安装失败：{e}")
        return 1
    print(f"安装完成：{app_dir}")
    print(f"启动方式：{app_dir / EXE_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
