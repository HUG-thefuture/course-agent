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
_STALL_MINUTES = 40  # 距上次进展超过该分钟数才放弃（大课一门可达 20 分钟）


def _ps(command: str, timeout: int = 60) -> str | None:
    """查询失败（偶发超时/启动失败）返回 None，调用方保守跳过本轮。"""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                             capture_output=True, text=True, timeout=timeout)
        return out.stdout
    except (subprocess.TimeoutExpired, OSError):
        return None


def batch_pids(match: str) -> list[int] | None:
    out = _ps("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              f"Where-Object {{ $_.CommandLine -match '{match}' }} | "
              "Select-Object -ExpandProperty ProcessId")
    if out is None:
        return None
    return [int(x) for x in out.split() if x.strip().isdigit()]


def partition_done(cfg: dict, work_file: Path) -> int:
    out_root = ROOT / cfg.get("output_dir", "output")  # 锚定项目根，不随 CWD 漂移
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
    # --source local：链首本地 qwen3-8b-16k（GPU 常驻 ~17s/调用）。
    # 用 api 时远程链（AMD 网关故障窗 / nvidia 76-420s）每调用先白等再回落，吞吐减半。
    cmd = [sys.executable, "main.py", "batch", "--config", "config.json",
           "--source", "local", "--retry-until-pass", "2",
           "--input", str(work_file), "--outdir", str(
               ROOT / cfg.get("output_dir", "output"))]
    proc = subprocess.Popen(cmd, cwd=str(ROOT))
    return proc.pid


def cfg_out(cfg: dict) -> str:
    return cfg.get("output_dir", "output")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="出题流水线总指挥")
    ap.add_argument("--work-dir", default="data")
    args = ap.parse_args(argv)

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
            st = state.setdefault(k, {"progress": 0, "last_progress_ts": 0,
                                      "gave_up": False})
            if st.get("gave_up"):
                continue
            done = partition_done(cfg, w)
            total = totals[k]
            if done >= total:
                continue
            all_settled = False
            pids = batch_pids(f"work_{k}.list")
            if pids is None:
                print(f"[{k}] 进程查询失败，本轮跳过（保守不重启不放弃）", flush=True)
                continue
            if pids:
                if done > st.get("progress", 0):  # 有进展即刷新时间戳
                    st["progress"] = done
                    st["last_progress_ts"] = time.time()
                continue
            # 进程不在：有进展 → 立即重启；无进展且超时窗已过 → 放弃
            now = time.time()
            stalled = (done <= st.get("progress", 0)
                       and st.get("last_progress_ts", 0) > 0
                       and now - st["last_progress_ts"] > _STALL_MINUTES * 60)
            if stalled:
                (data_dir / f"work_{k}.skipped").write_text(
                    time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
                st["gave_up"] = True
                print(f"[{k}] {int((now - st['last_progress_ts'])/60)} 分钟无进展，放弃该分区",
                      flush=True)
                continue
            if done > st.get("progress", 0):
                # 进程死亡期间取得的进展同样刷新时间窗（先更新再比较恒 False 的写法已废弃）
                st["progress"] = done
                st["last_progress_ts"] = now
            else:
                st.setdefault("last_progress_ts", now)
            print(f"[{k}] 重启分区（已完成 {done}/{total}）", flush=True)
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
