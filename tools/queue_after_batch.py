"""守望器：等待批量出题进程结束后，自动接力 视频转写 -> 最终批量补跑。

用法（后台运行）：
  python tools/queue_after_batch.py

判定方式：轮询 python.exe 进程命令行，无 "main.py batch" 即认为批量已结束。
16G 内存机器上 qwen3:8b 与 whisper 不能并行（会 OOM），故必须串行接力。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_PS = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
       "Select-Object -ExpandProperty CommandLine")


def batch_running() -> bool:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", _PS],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return True  # 查询失败时保守视为仍在运行
    return ("main.py" in out) and ("batch" in out)


def run(cmd: list[str]) -> int:
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    proc = subprocess.run([sys.executable, *cmd], cwd=str(ROOT))
    return proc.returncode


def main() -> int:
    poll = 600
    waited = 0.0
    print("守望启动：等待批量出题进程结束（每 10 分钟检查一次）", flush=True)
    while batch_running():
        time.sleep(poll)
        waited += poll
        print(f"批量仍在运行，已等待 {waited/3600:.1f} 小时", flush=True)
    print("批量进程已结束，开始视频转写（下载已在本地，直接转写）", flush=True)

    rc1 = run(["tools/video_transcribe.py",
               "--courses", "data/video_courses.json",
               "--urls", "data/video_urls",
               "--out", "data/harvest_transcripts",
               "--videos", "data/videos"])
    print(f"视频转写结束（rc={rc1}），启动最终批量补跑", flush=True)

    rc2 = run(["main.py", "batch",
               "--input", "data/harvest_transcripts",
               "--outdir", "output/特训营全部课程"])
    print(f"\n全部完成：视频转写 rc={rc1}，最终批量 rc={rc2}", flush=True)
    return 0 if (rc1 == 0 and rc2 == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
