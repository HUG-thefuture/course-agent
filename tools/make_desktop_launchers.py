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
rem 已有实例在运行(端口通)则只打开浏览器, 不再启动第二个服务
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', 8686); $c.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    start "" http://127.0.0.1:8686
    exit /b 0
)
cd /d "{root}"
rem --no-browser: 浏览器统一由本脚本打开(仅一次), 避免服务自己再开一个
start "CourseAgent" /min "{pythonw}" -m course_agent.web --port 8686 --no-browser
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8686
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
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        # 兜底：常见安装位置
        for cand in (Path(r"C:\Python314\pythonw.exe"),
                     Path(r"C:\Python311\pythonw.exe")):
            if cand.is_file():
                pythonw = cand
                break
        else:
            pythonw = Path("pythonw.exe")
    # 不用 .format()——bat 里的 PowerShell 代码含 { } 会被误当占位符
    text = START_BAT.replace("{root}", str(ROOT)).replace("{pythonw}", str(pythonw))
    write_bat(DESKTOP / "CourseAgent图形版.bat", text)
    write_bat(DESKTOP / "CourseAgent停止.bat", STOP_BAT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
