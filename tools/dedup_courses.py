"""去除重复课程：同一条回放（或同一门课）被反复整理出多份产物时，只保留最好的一份。

重复是怎么来的：
  - 同一回放链接跑多次（中途失败重试、手动再点一次"开始整理"）；
  - 早先解析器把"日期: ..."粘进标题，导致同一门课标题不一致，去重更看不出来。

判重依据（满足任一即视为同一门课）：
  1) 归一化标题相同：去掉标点、日期、录制序号后比较（`131-88-作业讲评` ≡ `88、作业讲评`）；
  2) 同一回放短码：`/crm/<短码>` 相同即同一条录制。

保留策略（依次比较）：质检通过优先 → 得分高 → 章节多 → 习题多 → 修改时间新。
只删"确定是重复"的多余份；不同标题不同来源的课程永不合并。

安全措施：
  - 默认 dry-run，只打印计划；
  - --apply 时把重复目录**移到 data/dup_trash/<时间戳>/**（可还原），不直接删除；
  - 同步把被删课程从 output/课程整合/*/组.json 与 data/group_vecs 缓存里剔除。

用法：
  python tools/dedup_courses.py                 # 预览
  python tools/dedup_courses.py --apply         # 隔离重复项
  python tools/dedup_courses.py --apply --purge # 直接永久删除（不推荐）
  python tools/dedup_courses.py --restore data/dup_trash/<时间戳>   # 还原某次隔离
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GROUPS_SUBDIR = "课程整合"
TRASH_ROOT = ROOT / "data" / "dup_trash"


# ------------------------------------------------------------------ 收集
def iter_courses(out_root: Path) -> list[Path]:
    """output 下所有含 session.json 的目录（顶层 + 一层子目录）。"""
    found: list[Path] = []
    if not out_root.is_dir():
        return found
    for d in sorted(out_root.iterdir()):
        if not d.is_dir() or d.name == GROUPS_SUBDIR:
            continue
        if (d / "session.json").is_file():
            found.append(d)
        else:
            for sub in sorted(d.iterdir()):
                if sub.is_dir() and (sub / "session.json").is_file():
                    found.append(sub)
    return found


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def norm_title(title: str) -> str:
    """归一化标题：去掉标点/日期/时间/录制序号，只剩可比较的词集合。

    `131-88-作业讲评-0928_0921` 与 `88、作业讲评 0928_0921` 归一化后相同；
    `32、langchain-agent及FunctionCalling日期: 2026-10-28 09:12:37`
    去掉日期段后与 `32、langchain-agent及FunctionCalling` 相同。
    """
    t = (title or "").lower()
    # 去掉 "日期: 2026-10-28 09:12:37" 这类尾巴
    t = re.sub(r"日期\s*[:：].*$", " ", t)
    t = re.sub(r"\d{2,4}[-_/]\d{1,2}[-_/]\d{1,2}(?:[ t]\d{1,2}[:：]\d{2}(?::\d{2})?)?", " ", t)
    t = re.sub(r"[^\w\u4e00-\u9fff]+", " ", t).strip()
    toks = t.split()
    # 去掉纯数字的录制序号/日期残片（但保留课号，如 "88"）
    toks = [x for x in toks if not re.fullmatch(r"(19|20)\d{2}", x)]
    # `131-88-作业讲评` -> 前两段都是数字时丢掉第一段（录制序号）
    if len(toks) >= 3 and toks[0].isdigit() and toks[1].isdigit():
        toks = toks[1:]
    return " ".join(sorted(toks))


def crm_code(source: str) -> str:
    m = re.search(r"/crm/([A-Za-z0-9]+)", source or "")
    return m.group(1) if m else ""


def course_info(d: Path) -> dict:
    s = read_json(d / "session.json")
    try:
        mtime = (d / "session.json").stat().st_mtime
    except OSError:
        mtime = 0.0
    return {
        "dir": d,
        "title": s.get("title") or d.name,
        "passed": bool(s.get("passed")),
        "score": int(s.get("qa_score") or 0),
        "sections": int(s.get("sections") or 0),
        "exercises": int(s.get("exercises") or 0),
        "source": (s.get("source") or "").strip(),
        "mtime": mtime,
        "key_title": norm_title(s.get("title") or d.name),
        "key_source": crm_code(s.get("source") or ""),
    }


def rank_key(rec: dict):
    """排序键（越大越好）：通过 > 得分 > 章节 > 习题 > 新。"""
    return (rec["passed"], rec["score"], rec["sections"], rec["exercises"], rec["mtime"])


def find_duplicate_groups(courses: list[dict]) -> list[list[dict]]:
    """并查集：标题键相同 或 回放短码相同 → 归为同一组。"""
    parent = list(range(len(courses)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    by_title: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for i, c in enumerate(courses):
        kt = c["key_title"]
        if kt:
            if kt in by_title:
                union(i, by_title[kt])
            else:
                by_title[kt] = i
        ks = c["key_source"]
        if ks:
            if ks in by_source:
                union(i, by_source[ks])
            else:
                by_source[ks] = i

    buckets: dict[int, list[dict]] = {}
    for i, c in enumerate(courses):
        buckets.setdefault(find(i), []).append(c)
    return [v for v in buckets.values() if len(v) > 1]


# ------------------------------------------------------------------ 清理副作用
def drop_from_groups(out_root: Path, removed_ids: set[str], dry: bool) -> list[str]:
    """把被移除课程从组.json 里摘掉；返回被修改的组名。"""
    dest = out_root / GROUPS_SUBDIR
    if not dest.is_dir():
        return []
    touched = []
    for gdir in sorted(dest.iterdir()):
        gp = gdir / "组.json"
        if not gdir.is_dir() or not gp.is_file():
            continue
        data = read_json(gp)
        members = data.get("courses") or []
        kept = [c for c in members if c not in removed_ids]
        if len(kept) != len(members):
            touched.append(data.get("name") or gdir.name)
            if not dry:
                data["courses"] = kept
                gp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    return touched


def drop_vec_cache(course_ids: set[str], dry: bool) -> int:
    """删掉被移除课程的块向量缓存（data/group_vecs/<md5>.json + .npy）。"""
    import hashlib

    cache = ROOT / "data" / "group_vecs"
    if not cache.is_dir():
        return 0
    n = 0
    for cid in course_ids:
        h = hashlib.md5(cid.encode("utf-8")).hexdigest()[:16]
        for ext in (".json", ".npy"):
            p = cache / f"{h}{ext}"
            if p.is_file():
                n += 1
                if not dry:
                    p.unlink(missing_ok=True)
    return n


def move_to_trash(dirs: list[Path], out_root: Path, dry: bool) -> Path | None:
    """把目录移到 data/dup_trash/<时间戳>/ 下（保留相对路径结构，便于还原）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest_root = TRASH_ROOT / stamp
    if not dry:
        dest_root.mkdir(parents=True, exist_ok=True)
    for d in dirs:
        rel = d.relative_to(out_root)
        target = dest_root / rel
        if dry:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(target))
    return dest_root


def restore(trash_dir: Path, out_root: Path) -> int:
    """把某次隔离的目录移回 output。返回成功还原的课程数。"""
    trash_dir = Path(trash_dir)
    if not trash_dir.is_dir():
        print(f"找不到隔离目录: {trash_dir}")
        return 0
    n = 0
    for d in sorted(trash_dir.rglob("*")):
        if not d.is_dir() or not (d / "session.json").is_file():
            continue
        rel = d.relative_to(trash_dir)
        target = out_root / rel
        if target.exists():
            print(f"  跳过（目标已存在）: {rel}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(target))
        n += 1
        print(f"  已还原: {rel}")
    print(f"\n共还原 {n} 门课程")
    return n


# ------------------------------------------------------------------ 主流程
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="去除重复课程")
    ap.add_argument("--outdir", default=str(ROOT / "output"))
    ap.add_argument("--apply", action="store_true", help="真正隔离（默认只预览）")
    ap.add_argument("--purge", action="store_true", help="隔离时直接永久删除，不进回收站")
    ap.add_argument("--restore", default="", help="还原某次隔离：传 data/dup_trash/<时间戳>")
    args = ap.parse_args(argv[1:])

    out_root = Path(args.outdir).resolve()

    if args.restore:
        n = restore(Path(args.restore), out_root)
        return 0 if n >= 0 else 1

    dirs = iter_courses(out_root)
    if not dirs:
        print(f"{out_root} 下没有课程产物")
        return 0

    courses = [course_info(d) for d in dirs]
    groups = find_duplicate_groups(courses)
    if not groups:
        print(f"扫描 {len(courses)} 门课程，未发现重复")
        return 0

    to_remove: list[Path] = []
    print(f"扫描 {len(courses)} 门课程，发现 {len(groups)} 组重复：\n")
    for g in sorted(groups, key=lambda v: -len(v)):
        kept, *rest = sorted(g, key=rank_key, reverse=True)
        print(f"--- {kept['title']}  （{len(g)} 份）")
        print(f"  [保留] {kept['score']:>3}分 通过={kept['passed']} "
              f"{kept['sections']}章{kept['exercises']}题  {kept['dir'].relative_to(out_root)}")
        for r in rest:
            print(f"  [去除] {r['score']:>3}分 通过={r['passed']} "
                  f"{r['sections']}章{r['exercises']}题  {r['dir'].relative_to(out_root)}")
            to_remove.append(r["dir"])
        print()

    removed_ids = {d.relative_to(out_root).as_posix() for d in to_remove}
    print(f"合计：保留 {len(courses) - len(to_remove)} 门，去除 {len(to_remove)} 门重复产物")

    dry = not args.apply
    touched = drop_from_groups(out_root, removed_ids, dry)
    nvec = drop_vec_cache(removed_ids, dry)
    if touched:
        print(f"受影响课程组：{', '.join(touched)}（成员列表已{'将' if dry else ''}剔除这些课程）")
    if nvec:
        print(f"块向量缓存：{'将' if dry else ''}清理 {nvec} 个文件")

    if dry:
        print("\n这是预览。确认无误后执行：python tools/dedup_courses.py --apply")
        print("（默认移入 data/dup_trash/，可随时 --restore 还原）")
        return 0

    if args.purge:
        for d in to_remove:
            shutil.rmtree(d, ignore_errors=True)
        print(f"\n已永久删除 {len(to_remove)} 个重复目录")
        return 0

    dest = move_to_trash(to_remove, out_root, dry=False)
    print(f"\n已把 {len(to_remove)} 个重复目录移入 {dest}")
    print(f"如需还原：python tools/dedup_courses.py --restore \"{dest}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
