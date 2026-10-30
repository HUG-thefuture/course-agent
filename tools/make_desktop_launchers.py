"""生成桌面启动器（CourseAgent图形版.bat / CourseAgent停止.bat）。

关键坑：cmd 解析 .bat 要求 ANSI(GBK) 编码 + CRLF 换行。UTF-8/LF 会导致
中文路径变乱码、命令被截断（报 "'p' 不是内部或外部命令" 之类）。

用法：python tools/make_desktop_launchers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = Path.home() / "Desktop"

START_BAT = r"""@echo off
cd /d "{root}"
rem 交给 launch_gui.py 判断：现有实例的代码版本是否与磁盘一致。
rem 一致 → 只开浏览器；不一致（改了代码但旧进程还在跑）→ 起新实例再开。
rem 这一步很关键：以前"端口通就只开浏览器"，导致旧进程一直服务，改动永远不生效。
"{python}" "tools\launch_gui.py" --port 8686
"""

STOP_BAT = r"""@echo off
taskkill /f /im pythonw.exe >nul 2>&1
echo CourseAgent 图形界面已停止
timeout /t 2 >nul
"""


def write_bat(path: Path, content: str) -> None:
    # cmd 要求：GBK(系统ANSI)编码 + CRLF 换行
    data = content.replace("\n", "\r\n").encode("gbk")
    path.write_bytes(data)
    print(f"已写入 {path}（{len(data)} 字节，GBK/CRLF）")


def main() -> int:
    # 启动器用控制台版 python（要显示"复用/重启"提示），pythonw 留作服务进程
    python = Path(sys.executable)
    if not python.is_file():
        python = Path(r"C:\Python314\python.exe")
    # 不用 .format()——bat 里的 PowerShell 代码含 { } 会被误当占位符
    text = START_BAT.replace("{root}", str(ROOT)).replace("{python}", str(python))
    write_bat(DESKTOP / "CourseAgent图形版.bat", text)
    write_bat(DESKTOP / "CourseAgent停止.bat", STOP_BAT)
    print("提示：若桌面已有旧版启动器，运行本脚本后即被覆盖为新版逻辑")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
