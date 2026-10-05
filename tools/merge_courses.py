"""课程整合：相关课程聚合为课程组，每组生成一份整合复习文档。

分组策略（两层）：
1. 编号映射：课程标题自带编号（如"45、request获取静态数据"）按系列分段；
2. 关键词兜底：无编号的课程（特训营/星星会议室）按笔记关键词归类。

每组产出：output/课程整合/<组名>/整合复习.md
（体系全景图 + 依赖关系 + 核心主线 + 易混淆对照 + 跨课程学习顺序）

用法（后台运行）：python tools/merge_courses.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_agent.config import load_config  # noqa: E402
from course_agent.utils import truncate  # noqa: E402
from course_agent.llm.factory import build_client_chains  # noqa: E402

# 编号段 -> 课程组（按用户课表的系列顺序）
SERIES = [
    (1, 13, "Web 前端基础：HTML 与 CSS"),
    (14, 17, "JavaScript 语言基础"),
    (18, 24, "Node/Express 后端与 Ajax"),
    (25, 39, "Python FastAPI 后端开发"),
    (40, 48, "爬虫与数据采集"),
    (49, 74, "数据分析与可视化（Pandas 栈）"),
    (75, 88, "Vue3 前端框架"),
]
DEFAULT_GROUP = "人工智能产训特训营"
KEYWORDS = [
    (("人工智能产训", "星星的个人会议室", "ACA考前集训"), DEFAULT_GROUP),
    (("vue", "pinia", "vue-router"), "Vue3 前端框架"),
    (("pandas", "numpy", "matplotlib", "seaborn", "数据分析"), "数据分析与可视化（Pandas 栈）"),
    (("selenium", "爬虫", "cookie", "xpath", "request"), "爬虫与数据采集"),
    (("fastapi", "sqlalchemy", "新闻app", "接口"), "Python FastAPI 后端开发"),
    (("express", "mysql", "node"), "Node/Express 后端与 Ajax"),
    (("html", "css", "flex", "盒模型"), "Web 前端基础：HTML 与 CSS"),
]


def group_of(title: str) -> str:
    m = re.match(r"^(\d{1,3})[、.．-]", title.strip())
    if m:
        num = int(m.group(1))
        for lo, hi, name in SERIES:
            if lo <= num <= hi:
                return name
    low = title.lower()
    for kws, name in KEYWORDS:
        if any(k in low for k in kws):
            return name
    return DEFAULT_GROUP


def _course_text(d: Path) -> str:
    try:
        secs = json.loads((d / "sections.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    parts = []
    for sec in secs:
        for kp in sec.get("knowledge_points", []):
            parts.append(kp.get("title", "") + " " +
                         (kp.get("concept") or kp.get("detail", ""))[:120])
    return " ".join(parts)[:4000]


def content_clusters(courses: list[Path], embedder, threshold: float = 0.55
                     ) -> list[list[Path]]:
    """按课程内容向量贪心聚类（余弦相似度 >= threshold 并组）。"""
    import math
    texts = {d: _course_text(d) for d in courses}
    embd = {d: embedder.embed([t or "空"])[0] for d, t in texts.items()}

    def cos(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(x * x for x in b)) or 1.0
        return dot / (na * nb)

    clusters: list[list[Path]] = []
    for d in courses:
        best, best_s = None, threshold
        for cl in clusters:
            s = sum(cos(embd[d], embd[m]) for m in cl) / len(cl)
            if s > best_s:
                best, best_s = cl, s
        if best is not None:
            best.append(d)
        else:
            clusters.append([d])
    return clusters


def cluster_name(courses: list[Path], seed_groups: dict[str, list[Path]]) -> str:
    """组名：取组内多数课程所属的种子组名，否则用首个课程标题。"""
    from collections import Counter
    names = []
    for d in courses:
        for g, ds in seed_groups.items():
            if d in ds:
                names.append(g)
    if names:
        return Counter(names).most_common(1)[0][0]
    try:
        return json.loads((courses[0] / "session.json").read_text(
            encoding="utf-8")).get("title", "课程组")[:24]
    except (json.JSONDecodeError, OSError):
        return "课程组"


def collect_groups(output_root: Path) -> dict[str, list[Path]]:
    """扫描产物目录，按组聚合（目录名/标题双重判断）。"""
    groups: dict[str, list[tuple[Path, int]]] = {}
    for d in sorted(p for p in output_root.iterdir()
                    if p.is_dir() and (p / "sections.json").is_file()):
        title = ""
        try:
            title = json.loads((d / "session.json").read_text(
                encoding="utf-8")).get("title", "")
        except (json.JSONDecodeError, OSError):
            pass
        name = group_of(title or d.name)
        # 排序键：标题里的编号（无编号排最后按日期）
        m = re.search(r"(\d{1,3})[、.．-]", title)
        order = int(m.group(1)) if m else 9999
        groups.setdefault(name, []).append((d, order))
    for name in groups:
        groups[name].sort(key=lambda x: x[1])
    return {k: [d for d, _ in v] for k, v in groups.items()}


def group_context(courses: list[Path], per_course: int = 3) -> str:
    """每组上下文：每门课取前 N 个知识点标题+概念摘要。"""
    parts = []
    for d in courses:
        try:
            secs = json.loads((d / "sections.json").read_text(encoding="utf-8"))
            title = json.loads((d / "session.json").read_text(
                encoding="utf-8")).get("title", d.name)
        except (json.JSONDecodeError, OSError):
            continue
        lines = []
        for sec in secs:
            for kp in sec.get("knowledge_points", [])[:per_course]:
                concept = truncate(kp.get("concept") or kp.get("detail", ""), 90)
                lines.append(f"  - {kp.get('title', '')}：{concept}")
        parts.append(f"《{truncate(title, 40)}》\n" + "\n".join(lines[:12]))
    return "\n\n".join(parts)


def gen_integration(llm, group_name: str, courses: list[Path]) -> str:
    ctx = group_context(courses)
    system = (
        "你是双擎学习助手的跨课程整合模块。以下是同一课程体系下的多门相关课程"
        "的知识点摘要。生成一份**整合复习文档**（Markdown），结构：\n"
        f"# {group_name} · 整合复习\n"
        "## 课程构成\n(列出组内课程及各自定位, 一句话)\n"
        "## 知识体系全景图\n(文字树状图, 跨课程组织)\n"
        "## 学习主线\n(3-6 步主线, 说明先后依赖)\n"
        "## 核心知识点速览\n(按主题分组列出要点, 标注来自哪门课)\n"
        "## 易混淆点对照表\n| 概念A | 概念B | 区别 | 记忆方法 |\n"
        "## 跨课程学习顺序建议\n(编号路径, 标注可并行项)\n"
        "## 综合自检清单\n(逐知识点检查项)\n\n"
        "纪律：只使用【课程素材】中出现的内容；补充标注「（补充，非课程内容）」。\n\n"
        f"【课程素材】\n{truncate(ctx, 24000)}")
    return llm.chat(system, "请生成整合复习文档。", max_tokens=4096)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="课程整合：相关课程聚合生成整合复习")
    ap.add_argument("--output", default="output/特训营全部课程")
    ap.add_argument("--dest", default="output/课程整合")
    ap.add_argument("--only", default="", help="只处理指定组名（调试用）")
    ap.add_argument("--by-title", action="store_true",
                    help="仅按标题分组（默认按内容向量聚类+标题命名）")
    ap.add_argument("--threshold", type=float, default=0.55)
    args = ap.parse_args(argv)

    cfg = load_config()
    local_chain, cloud_chain = build_client_chains(cfg)
    from course_agent.llm import Router
    llm = Router(cloud_chain + local_chain)

    all_courses = sorted(p for p in Path(args.output).iterdir()
                         if p.is_dir() and (p / "sections.json").is_file())
    seed = collect_groups(Path(args.output))
    if args.by_title:
        groups = seed
    else:
        from course_agent.kb_vector import OllamaEmbedder
        emb = OllamaEmbedder()
        print(f"内容聚类 {len(all_courses)} 门课程（阈值 {args.threshold}）…", flush=True)
        clusters = content_clusters(all_courses, emb, args.threshold)
        groups = {}
        for cl in clusters:
            if not cl:
                continue
            gname = cluster_name(cl, seed)
            # 同名组合并
            groups.setdefault(gname, []).extend(cl)
        print(f"内容聚类得到 {len(groups)} 组："
              f"{[(k, len(v)) for k, v in groups.items()]}", flush=True)
    if args.only:
        groups = {k: v for k, v in groups.items() if k == args.only}

    for name, courses in groups.items():
        dest = Path(args.dest) / re.sub(r"[^\w（）()-]+", "-", name)
        dest.mkdir(parents=True, exist_ok=True)
        md = dest / "整合复习.md"
        if md.is_file() and md.stat().st_size > 500:
            print(f"[{name}] 已存在，跳过", flush=True)
            continue
        try:
            text = gen_integration(llm, name, courses)
            md.write_text(text, encoding="utf-8")
            print(f"[{name}] 完成（{len(courses)} 门课整合）-> {md}", flush=True)
        except Exception as e:
            print(f"[{name}] 失败: {truncate(str(e), 120)}", flush=True)
        time.sleep(1)
    print("\n课程整合全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
