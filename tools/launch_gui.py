"""CourseAgent 图形界面启动器（供桌面 .bat 调用）。

解决的问题：GUI 用 pythonw 常驻后台，源码更新后旧进程仍占着 8686 端口对外服务，
用户看到的行为还是修之前的（表现为"改了没用""功能依旧不可用"）。以前 .bat 的逻辑是
「端口通就直接打开浏览器」，于是永远打开那个旧实例。

本脚本的逻辑：
  1. 探测 8686 端口上的实例，读 /api/build 比对「运行中代码版本」与「磁盘代码版本」；
  2. 版本一致 → 直接打开浏览器（复用现有实例）；
  3. 版本不一致（含老版本没这个接口的 404 情况）→ 后台起一个新实例（端口自动顺延），
     等它写出 data/webui_url.txt，再用那个地址打开浏览器；
  4. 端口没人听 → 直接起新实例。

用法：python tools/launch_gui.py [--port 8686] [--no-browser]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_agent.buildinfo import build_id  # noqa: E402
from course_agent.config import load_config  # noqa: E402
from course_agent.net import open_direct  # noqa: E402


def _get_json(url: str, timeout: float = 2.5):
    try:
        with open_direct(urllib.request.Request(url), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def probe(port: int) -> dict | None:
    """返回该端口上的实例信息；无实例返回 None。"""
    base = f"http://127.0.0.1:{port}"
    info = _get_json(base + "/api/build", timeout=2.5)
    if isinstance(info, dict) and "running" in info:
        info["url"] = base
        return info
    # 老版本没有 /api/build：能响应课程列表也算活着（但代码一定旧）
    if _get_json(base + "/api/courses", timeout=2.5) is not None:
        return {"running": None, "disk": build_id(), "stale": True, "url": base}
    return None


def listen_pid(port: int) -> int | None:
    """找出监听该端口的进程 PID（只用系统自带 netstat，不引额外依赖）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None
    needle = f":{port}"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or "LISTEN" not in line.upper():
            continue
        if parts[1].endswith(needle):
            try:
                return int(parts[-1])
            except ValueError:
                continue
    return None


def kill(pid: int) -> bool:
    """尽力结束旧进程；权限不足时返回 False，调用方继续走新端口。"""
    try:
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def wait_url(path: Path, timeout: float = 25.0) -> str:
    """等新实例写出监听地址（端口可能顺延），返回 URL。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            u = path.read_text(encoding="utf-8").strip()
        except OSError:
            u = ""
        if u:
            return u
        time.sleep(0.4)
    return ""


def spawn(port: int) -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.is_file() else Path(sys.executable)
    subprocess.Popen(
        [str(exe), "-m", "course_agent.web", "--port", str(port), "--no-browser"],
        cwd=str(ROOT), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def is_busy(port: int) -> bool:
    """该实例是否正在跑批量任务（跑着就绝不能杀，否则会毁掉进行中的整理）。"""
    info = _get_json(f"http://127.0.0.1:{port}/api/run/status", timeout=3.0)
    return bool(isinstance(info, dict) and info.get("running"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CourseAgent GUI 启动器")
    ap.add_argument("--port", type=int, default=8686)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config()
    url_path = Path(cfg.data_dir) / "webui_url.txt"

    info = probe(args.port)
    if info and not info.get("stale"):
        url = info.get("url") or f"http://127.0.0.1:{args.port}"
        print(f"复用运行中的实例（代码版本一致）：{url}")
    else:
        if info:
            print(f"检测到旧版本实例（运行中 {info.get('running')} / 磁盘 {info.get('disk')}）")
            pid = listen_pid(args.port)
            # 关键安全阀：正在跑任务的实例一律不动，否则会杀掉进行中的整理
            # （中途 kill 会丢掉已生成但未落盘的内容）。
            if is_busy(args.port):
                print("该实例正在处理课程任务，不重启它；新实例将在下一个端口启动，"
                      "可在两个窗口分别使用")
                pid = None
            elif pid and kill(pid):
                print(f"已结束旧实例 (pid={pid})，新实例将接管端口 {args.port}")
                time.sleep(1.0)
            elif pid:
                print(f"旧实例 (pid={pid}) 无法结束，新实例将换端口启动")
        try:
            url_path.unlink(missing_ok=True)   # 旧地址作废，避免读到上一轮的
        except OSError:
            pass
        spawn(args.port)
        url = wait_url(url_path)
        if not url:
            url = f"http://127.0.0.1:{args.port}"
            print("新实例未在 25 秒内就绪，尝试直接打开默认端口")
        else:
            print(f"新实例已就绪：{url}")

    if not args.no_browser:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
