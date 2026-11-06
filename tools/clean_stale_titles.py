"""一次性清理：移除 course_titles.json 中指向已不存在课程的失效覆盖项。

被移除项先完整备份到 data/cleanup_restore/<时间戳>/removed_title_overrides.json，
保证可还原。用法：python tools/clean_stale_titles.py [--apply]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import dedup_courses as dd  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--outdir", default=str(ROOT / "output"))
    ap.add_argument("--titles", default=str(ROOT / "data" / "course_titles.json"))
    args = ap.parse_args(argv[1:])

    titles_path = Path(args.titles)
    data = json.loads(titles_path.read_text(encoding="utf-8"))
    out_root = Path(args.outdir).resolve()
    dirs = dd.iter_courses(out_root)
    alive_ids = {d.relative_to(out_root).as_posix() for d in dirs}
    alive_names = {d.name for d in dirs}

    stale = {k: v for k, v in data.items()
             if k not in alive_ids and k not in alive_names}
    print(f"课程总数 {len(dirs)}，标题覆盖 {len(data)} 项，失效 {len(stale)} 项：")
    for k, v in stale.items():
        print(f"  - {k}  ->  {v}")

    if not stale:
        print("无需清理。")
        return 0
    if not args.apply:
        print("\n预览模式。执行请加 --apply")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "data" / "cleanup_restore" / stamp / "removed_title_overrides.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(stale, ensure_ascii=False, indent=2), encoding="utf-8")

    kept = {k: v for k, v in data.items() if k not in stale}
    titles_path.write_text(json.dumps(kept, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(f"\n已移除 {len(stale)} 项，保留 {len(kept)} 项。")
    print(f"备份（可还原）：{backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
