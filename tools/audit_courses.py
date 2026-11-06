"""课程数据一致性体检（只读，不改动任何文件）。

检查项：
  1. 重复课程（复用 dedup_courses 的判重逻辑）；
  2. 课程组 组.json 里的幽灵成员（指向已删课程）与组内重复；
  3. course_titles.json 里指向已不存在课程的覆盖项；
  4. webui_progress 里对应不到课程的孤儿答题进度；
  5. 残缺产物目录（缺 notes.md / sections.json 等关键文件）；
  6. 空目录；
  7. 未通过质检的课程（GUI 默认隐藏，可能有重跑价值）。

输出：人类可读的报告，以及可选的 JSON（--json）。
退出码：0 = 完全一致；1 = 发现问题（便于脚本告警）。

用法：
  python tools/audit_courses.py
  python tools/audit_courses.py --json > data/audit_report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import dedup_courses as dd  # noqa: E402


def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _progress_name(cid: str) -> str:
    """与 webui._progress_path 保持一致：非法字符换下划线。"""
    return re.sub(r"[^\w.-]", "_", cid) + ".json"


def audit(out_root: Path, data_dir: Path) -> dict:
    report: dict = {"courses": 0, "issues": {}}

    # 真实课程集合
    dirs = dd.iter_courses(out_root)
    alive_ids = {d.relative_to(out_root).as_posix() for d in dirs}
    alive_names = {d.name for d in dirs}
    report["courses"] = len(dirs)

    courses = [dd.course_info(d) for d in dirs]

    # 1) 重复
    groups = dd.find_duplicate_groups(courses)
    dups = [{"title": max(g, key=dd.rank_key)["title"],
             "count": len(g),
             "dirs": [str(c["dir"].relative_to(out_root)) for c in g]}
            for g in groups]
    report["issues"]["duplicates"] = dups

    # 2) 幽灵成员 + 组内重复
    ghosts = dd.find_ghost_members(out_root, alive_ids)
    report["issues"]["ghost_members"] = [
        {"group": gp.parent.name, "member": m} for gp, m, _ in ghosts]
    from collections import Counter
    intra = []
    for gp in (out_root / dd.GROUPS_SUBDIR).rglob("组.json"):
        cs = _load_json(gp).get("courses") or []
        doubled = [k for k, v in Counter(cs).items() if v > 1]
        if doubled:
            intra.append({"group": gp.parent.name, "members": doubled})
    report["issues"]["intra_group_duplicates"] = intra

    # 3) course_titles.json 失效覆盖
    ct = data_dir / "course_titles.json"
    stale_titles = []
    if ct.is_file():
        for k, v in (_load_json(ct) or {}).items():
            if k not in alive_ids and k not in alive_names:
                stale_titles.append({"id": k, "title": v})
    report["issues"]["stale_title_overrides"] = stale_titles

    # 4) 进度孤儿（按 webui 的命名规则反查，避免误报）
    valid_progress = {_progress_name(cid) for cid in alive_ids}
    orphans = []
    pdir = data_dir / "webui_progress"
    if pdir.is_dir():
        for p in sorted(pdir.glob("*.json")):
            if p.name not in valid_progress:
                orphans.append(p.name)
    report["issues"]["orphan_progress"] = orphans

    # 5) 残缺产物
    need = ("notes.md", "sections.json", "session.json")
    broken = []
    for d in dirs:
        miss = [n for n in need if not (d / n).is_file()]
        if miss:
            broken.append({"dir": str(d.relative_to(out_root)), "missing": miss})
    report["issues"]["incomplete"] = broken

    # 6) 空目录
    empties = [p.relative_to(out_root).as_posix() for p in out_root.rglob("*")
               if p.is_dir() and not any(p.iterdir())]
    report["issues"]["empty_dirs"] = empties

    # 7) 未通过质检
    failed = [{"dir": str(c["dir"].relative_to(out_root)),
               "title": c["title"], "score": c["score"]}
              for c in courses if not c["passed"]]
    report["issues"]["failed_qa"] = failed

    report["total_issues"] = sum(len(v) if isinstance(v, list) else 0
                                 for v in report["issues"].values())
    return report


def print_human(r: dict) -> None:
    print(f"课程总数: {r['courses']}")
    issues = r["issues"]
    labels = {
        "duplicates": "重复课程组",
        "ghost_members": "组.json 幽灵成员",
        "intra_group_duplicates": "组内重复成员",
        "stale_title_overrides": "失效标题覆盖",
        "orphan_progress": "孤儿答题进度",
        "incomplete": "残缺产物",
        "empty_dirs": "空目录",
        "failed_qa": "未通过质检",
    }
    for key, label in labels.items():
        rows = issues.get(key) or []
        print(f"  {label:<16} {len(rows):>3} 个" + ("  ✓" if not rows else ""))
        for row in rows[:6]:
            if isinstance(row, dict):
                desc = row.get("title") or row.get("dir") or row.get("member") or row.get("id") or ""
                extra = ""
                if "count" in row:
                    extra = f"（{row['count']} 份）"
                if "score" in row:
                    extra = f"（{row['score']} 分）"
                if "missing" in row:
                    extra = f"（缺 {', '.join(row['missing'])}）"
                print(f"      - {desc}{extra}")
            else:
                print(f"      - {row}")
        if len(rows) > 6:
            print(f"      … 另有 {len(rows) - 6} 个")
    print()
    if r["total_issues"] == 0:
        print("✓ 数据完全一致")
    else:
        print(f"发现 {r['total_issues']} 处提示（未通过质检与空目录属历史遗留，不一定需要修）")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="课程数据一致性体检（只读）")
    ap.add_argument("--outdir", default=str(ROOT / "output"))
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非人类可读报告")
    args = ap.parse_args(argv[1:])

    r = audit(Path(args.outdir).resolve(), Path(args.data_dir).resolve())
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print_human(r)
    # 只有「结构性错误」才算失败；未通过质检/空目录/孤儿进度仅提示
    hard = (len(r["issues"]["duplicates"]) + len(r["issues"]["ghost_members"])
            + len(r["issues"]["intra_group_duplicates"]) + len(r["issues"]["incomplete"]))
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
