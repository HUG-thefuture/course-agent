"""出题流水线总指挥：分区级监控、崩溃自动重启、无进展退避、完成自动收尾。

以持久后台进程运行（python tools/supervisor.py）：
  - 每 2 分钟轮询：任一分区无 batch 进程且仍有未完成课程 → 自动重启该分区
    （断点续跑：已完成课程自动跳过，重启零浪费）；
  - 无进展退避：分区连续 3 次重启无新增完成 → 放弃（写 <name>.skipped）；
  - 三分区全部"完成或放弃" → 课程智能改名 → 编程题重生成 → 结束。

用法：python tools/supervisor.py [--work-dir data]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_POLL = 120
_MAX_STALL = 5


def _ps(command: str, timeout: int = 60) -> str:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                         capture_output=True, text=True, timeout=timeout)
    return out.stdout


def batch_pids(match: str) -> list[int]:
    out = _ps("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              f"Where-Object {{ $_.CommandLine -match '{match}' }} | "
              "Select-Object -ExpandProperty ProcessId")
    return [int(x) for x in out.split() if x.strip().isdigit()]


def partition_done(cfg: dict, work_file: Path) -> int:
    out_root = Path(cfg.get("output_dir", "output"))
    done = {d.parent.name.split("-", 1)[-1] for d in out_root.rglob("session.json")}
    n = 0
    for line in work_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and Path(line).stem in done:
            n += 1
    return n


def partition_total(work_file: Path) -> int:
    return len([l for l in work_file.read_text(encoding="utf-8").splitlines()
                if l.strip()])


def spawn(work_file: Path, data_dir: Path, cfg: dict) -> int:
    cmd = [sys.executable, "main.py", "batch", "--config", "config.json",
           "--source", "api", "--retry-until-pass", "2",
           "--input", str(work_file), "--outdir", str(
               ROOT / cfg.get("output_dir", "output"))]
    proc = subprocess.Popen(cmd, cwd=str(ROOT))
    return proc.pid


def cfg_out(cfg: dict) -> str:
    return cfg.get("output_dir", "output")


def main() -> int:
    ap = argparse.ArgumentParser(description="出题流水线总指挥")
    ap.add_argument("--work-dir", default="data")
    args = ap.parse_args([])

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    data_dir = ROOT / args.work_dir
    out_root = ROOT / cfg_out(cfg)
    work_files = {k: data_dir / f"work_{k}.list" for k in ("a", "b", "c")}
    for w in work_files.values():
        if not w.is_file():
            print(f"分区清单缺失: {w}")
            return 1

    state_path = data_dir / "supervisor_state.json"
    state: dict = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}

    totals = {k: partition_total(w) for k, w in work_files.items()}
    print(f"总指挥启动：分区规模 {totals}（每 {_POLL} 秒轮询）", flush=True)

    while True:
        all_settled = True
        progress_any = False
        for k, w in work_files.items():
            st = state.setdefault(k, {"restarts": 0, "progress": 0,
                                      "gave_up": False})
            if st.get("gave_up"):
                continue
            done = partition_done(cfg, w)
            total = totals[k]
            if done >= total:
                continue
            all_settled = False
            pids = batch_pids(f"work_{k}.list")
            if pids:
                continue
            # 进程不在：判断是否有进展决定重启或放弃
            if st.get("progress", 0) >= done:
                st["restarts"] = st.get("restarts", 0) + 1
            else:
                st["restarts"] = 0
                st["progress"] = done
                progress_any = True
            if st["restarts"] >= _MAX_STALL:
                (data_dir / f"work_{k}.skipped").write_text(
                    time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
                st["gave_up"] = True
                print(f"[{k}] 连续 {_MAX_STALL} 次重启无进展，放弃该分区", flush=True)
                continue
            print(f"[{k}] 重启分区（已完成 {done}/{total}，"
                  f"第 {st['restarts'] + 1} 次重启）", flush=True)
            spawn(w, data_dir, cfg)
            time.sleep(5)
        # 持久化状态
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        if all_settled:
            break
        print(f"分区运行中，{_POLL} 秒后再查", flush=True)
        time.sleep(_POLL)

    print("=== 三分区已全部完成/放弃，执行收尾 ===", flush=True)
    print("=== 课程智能改名 ===", flush=True)
    subprocess.run([sys.executable, "tools/rename_courses.py",
                    "--output", str(out_root)], cwd=str(ROOT))
    print("=== 编程题重生成 ===", flush=True)
    subprocess.run([sys.executable, "tools/regen_exercises.py",
                    "--output", str(out_root),
                    "--transcripts", str(data_dir / "harvest_transcripts")],
                   cwd=str(ROOT))
    print("=== 全链完成 ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
