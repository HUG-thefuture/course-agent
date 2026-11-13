"""从 强化复习.md 恢复被旧 bug 抹掉的章节判分与解析。

背景：旧版 POST /api/progress 只做章节级合并，用户切章/刷新时只带 `user`
的暂存把该章已存的 score/correct 覆盖掉了；而每次交卷的判分摘要与解析其实
都追加写进了课程目录的 强化复习.md。本工具把它解析回 data/webui_progress/*.json。

用法：
    python tools/restore_quiz_progress.py --dry-run   # 只看会改什么
    python tools/restore_quiz_progress.py             # 实际写入
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from course_agent.webui import (_course_dirs, _course_id, _load_progress,  # noqa: E402
                               _progress_path, _save_progress)

_HEAD = re.compile(r"^##\s+(?P<title>.+?)\s*·\s*测验解析（(?P<stamp>[^）]+)）\s*$")
_SECTION_KV = re.compile(r"^\*\*(?P<k>[^*]+)\*\*[:：]\s*(?P<v>.*)$")


def parse_report(md: str) -> list[dict]:
    """把 强化复习.md 拆成多次测验记录（按 ## 标题分块，取每章最后一次）。"""
    lines = md.splitlines()
    blocks: list[dict] = []
    cur: dict | None = None
    for ln in lines:
        m = _HEAD.match(ln.strip())
        if m:
            cur = {"title": m.group("title").strip(), "stamp": m.group("stamp").strip(),
                   "overall": "", "items": [], "weak_points": [], "suggestions": []}
            blocks.append(cur)
            continue
        if cur is None:
            continue
        s = ln.strip()
        m2 = _SECTION_KV.match(s)
        if m2 and m2.group("k").strip() == "整体评价":
            cur["overall"] = m2.group("v").strip()
            continue
        # 逐题解析：- **第 N 题**：解析
        m3 = re.match(r"^-\s*\*\*第\s*(\d+)\s*题\*\*[:：]\s*(.*)$", s)
        if m3:
            cur["items"].append({"idx": int(m3.group(1)) - 1, "analysis": m3.group(2).strip()})
            continue
        # 薄弱点：- **主题**：内容
        m4 = re.match(r"^-\s*\*\*(.+?)\*\*[:：]\s*(.*)$", s)
        if m4:
            cur["weak_points"].append({"topic": m4.group(1).strip(),
                                       "content": m4.group(2).strip()})
            continue
        # 复习建议：- 文本（排在「复习建议」小节内才算）
        if s.startswith("- "):
            cur["suggestions"].append(s[2:].strip())
    return blocks


def _norm(s: str) -> str:
    return re.sub(r"[\s\-—·、，,。.：:（）()\u3000]", "", s or "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 强化复习.md 恢复测验进度缓存")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写入")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    from course_agent.config import load_config
    cfg = load_config(args.config)
    changed = 0
    for d in _course_dirs(cfg):
        md_path = d / "强化复习.md"
        if not md_path.is_file():
            continue
        try:
            blocks = parse_report(md_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not blocks:
            continue
        try:
            sections = json.loads((d / "sections.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # 章节标题 -> 索引（用规范化标题匹配，容忍标点/空白差异）
        title2idx = {_norm(s.get("title", "")): i for i, s in enumerate(sections)}
        # 每章取最后一次（最新）记录
        latest: dict[int, dict] = {}
        for b in blocks:
            si = title2idx.get(_norm(b["title"]))
            if si is None:
                # 退而求其次：包含匹配
                for t, i in title2idx.items():
                    if t and (t in _norm(b["title"]) or _norm(b["title"]) in t):
                        si = i
                        break
            if si is None:
                continue
            latest[si] = b
        if not latest:
            continue

        cid = _course_id(cfg, d)
        prog = _load_progress(cfg, cid)
        sums = prog.setdefault("summaries", {})
        revs = prog.setdefault("reviews", {})
        touched = False
        for si, b in latest.items():
            key = str(si)
            # 只补缺失的解析，不覆盖更新的记录
            if key not in revs or not revs[key].get("overall"):
                revs[key] = {"overall": b["overall"], "items": b["items"],
                             "weak_points": b["weak_points"],
                             "suggestions": b["suggestions"], "restored_from": "强化复习.md"}
                touched = True
            if key not in sums:
                sums[key] = {"graded": len(b["items"]),
                             "correct": None,  # 报告里没有逐题对错，只能留空
                             "avg": None,
                             "restored_from": "强化复习.md"}
                touched = True
        if touched:
            changed += 1
            print(f"{'[dry-run] ' if args.dry_run else ''}恢复 {cid}："
                  f"{len(latest)} 章解析")
            if not args.dry_run:
                _save_progress(cfg, cid, prog)

    print(f"\n{'将' if args.dry_run else '已'}处理 {changed} 门课程的缓存")
    print("提示：报告里没有逐题对错与分数，score/correct 无法凭空恢复——"
          "但用户原有作答文本一直保留，重新交卷即可补齐分数。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
