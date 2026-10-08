"""开机自愈：等待 Ollama 就绪，若总指挥未在运行则拉起出题流水线。

由 Windows 启动项（Startup/course-agent-selfheal.bat）在登录后静默触发，
也可以随时手动运行——脚本幂等：总指挥已在运行就直接退出，绝不重复拉起。

流程：
  1. 轮询 http://127.0.0.1:11434/api/tags 直到 Ollama 就绪（最多等 15 分钟，
     超时也继续——还有 AMD/商汤远程服务商兜底）；
  2. 按命令行匹配 supervisor.py 检测实例（含 pythonw），存在即退出；
  3. 旧 supervisor.log 轮转为 supervisor_prev.log，再 Start-Process 隐藏窗口
     拉起 tools/supervisor.py，输出重定向到 data/supervisor(.err).log。

日志：data/boot_selfheal.log（追加）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOG = DATA / "boot_selfheal.log"
OLLAMA = "http://127.0.0.1:11434/api/tags"
_WAIT_MAX = 15 * 60
_WAIT_POLL = 15

_PS_SUPERVISOR = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR "
                  "Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match "
                  "'supervisor[.]py' } | Select-Object -ExpandProperty ProcessId")


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        DATA.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def ollama_ready() -> bool:
    try:
        with urllib.request.urlopen(OLLAMA, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("models"))
    except Exception:
        return False


def supervisor_running() -> bool:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", _PS_SUPERVISOR],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return True  # 查询失败保守视为在运行，宁可不拉也不重拉
    return any(x.isdigit() for x in out.split())


def spawn_supervisor(python_exe: Path) -> bool:
    out_log = DATA / "supervisor.log"
    err_log = DATA / "supervisor_err.log"
    prev = DATA / "supervisor_prev.log"
    try:
        if out_log.is_file():
            out_log.replace(prev)  # 保留上一轮日志供追溯
    except OSError:
        pass
    cmd = (f"Start-Process -FilePath '{python_exe}' -ArgumentList 'tools/supervisor.py' "
           f"-WorkingDirectory '{ROOT}' -WindowStyle Hidden "
           f"-RedirectStandardOutput '{out_log}' -RedirectStandardError '{err_log}'")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def main() -> int:
    log("=== boot_selfheal 启动 ===")
    waited = 0
    while not ollama_ready() and waited < _WAIT_MAX:
        time.sleep(_WAIT_POLL)
        waited += _WAIT_POLL
    if waited:
        log(f"Ollama 等待 {waited // 60} 分钟后" + ("就绪" if ollama_ready() else "仍未就绪（继续拉起，靠服务商兜底）"))
    else:
        log("Ollama 已就绪" if ollama_ready() else "Ollama 未就绪且立即超时（异常，继续）")

    if supervisor_running():
        log("总指挥已在运行，跳过")
        return 0
    python_exe = Path(sys.executable).with_name("python.exe")
    if not python_exe.is_file():
        python_exe = Path(sys.executable)
    if spawn_supervisor(python_exe):
        log("总指挥已拉起")
        return 0
        log("拉起失败，60 秒后重试一次")
    time.sleep(60)
    if supervisor_running():
        log("重试检测：总指挥已在运行，跳过")
        return 0
    return 0 if spawn_supervisor(python_exe) else 1


if __name__ == "__main__":
    raise SystemExit(main())
