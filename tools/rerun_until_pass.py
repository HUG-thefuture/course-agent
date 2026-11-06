"""把单门课重跑到通过质检为止（对齐 batch.retry_until_pass 的持续迭代语义）。

fix 场景：audit 报出的「未通过质检」课程，需要就地重跑（写回原目录），
而 batch 会自建目录名。本脚本循环调用 `main.py run`，每次是全新进程 =
全新的模型健康状态与审查抽样，直到 session.json 的 passed=true。

- 原 session.json 先备份到 --backup，失败可还原；
- 每轮日志写 --log-prefix.<n>.log；
- 返回码 0=通过，1=用尽轮数仍未通过。

用法：
  python tools/rerun_until_pass.py \
      --input data/harvest_transcripts/001-....json \
      --outdir output/01-001-.... \
      --title "001-..." --runs 5 --max-attempts 4
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--runs", type=int, default=5, help="最多独立重跑次数")
    ap.add_argument("--max-attempts", type=int, default=4,
                    help="传给 run 的管线内层循环上限")
    ap.add_argument("--source", default="", help="可选 --source 值")
    ap.add_argument("--backup", default="", help="原 session.json 备份路径")
    ap.add_argument("--log-prefix", default="", help="每轮日志前缀")
    args = ap.parse_args(argv[1:])

    outdir = Path(args.outdir)
    session = outdir / "session.json"
    backup = Path(args.backup) if args.backup else (
        ROOT / "data" / f"rerun_backup_{time.strftime('%Y%m%d-%H%M%S')}.json")
    prefix = Path(args.log_prefix) if args.log_prefix else (
        ROOT / "data" / "rerun_attempt")

    if session.is_file():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(session, backup)
        print(f"已备份原 session.json -> {backup}", flush=True)

    for i in range(1, args.runs + 1):
        cmd = [sys.executable, "-u", "main.py", "run",
               "--input", args.input, "--title", args.title,
               "--outdir", args.outdir,
               "--max-attempts", str(args.max_attempts), "-v"]
        if args.source:
            cmd += ["--source", args.source]
        print(f"\n=== 独立重跑 {i}/{args.runs}: {' '.join(cmd)} ===", flush=True)
        log_path = prefix.parent / f"{prefix.name}.{i}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as lf:
            proc = subprocess.run(cmd, cwd=str(ROOT), stdout=lf,
                                  stderr=subprocess.STDOUT)
        print(f"    exit={proc.returncode}  日志: {log_path}", flush=True)

        if session.is_file():
            try:
                j = json.loads(session.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                j = {}
            print(f"    passed={j.get('passed')} score={j.get('qa_score')} "
                  f"sections={j.get('sections')}", flush=True)
            if j.get("passed"):
                print(f"\n✓ 第 {i} 轮通过质检（得分 {j.get('qa_score')}）", flush=True)
                return 0

    print(f"\n✗ {args.runs} 轮后仍未通过；原产物备份在 {backup}", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
