"""课程整合：相关课程聚合为课程组，每组生成一份整合复习文档。

分组策略（默认：按内容相关性）：
内容聚类——每门课的知识点逐块嵌入（bge-m3），课程间相似度用「双向 top-k
块相似度均值」（抗整课平均掉的局部强信号），贪心平均连接聚类；组名取组内
多数课程所属的编号系列名（编号映射/关键词作为命名种子）。

--by-title 退回纯标题编号分组（不跑嵌入，快，但无编号课全归"特训营"）。

每组产出：output/课程整合/<组名>/整合复习.md + 组.json（成员清单，webui 读）
（体系全景图 + 依赖关系 + 核心主线 + 易混淆对照 + 跨课程学习顺序）

用法（后台运行）：python tools/merge_courses.py [--force]
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


def course_number(title: str) -> int | None:
    """从标题提取课程编号（对应 SERIES 编号空间，非录制目录序号）。

    - "072-35-新闻APP…"：首段是录制序号，第二段才是课号 → 35；
    - "45、request…"：首段即课号 → 45；
    - "001-人工智能产训特训-…"：无第二编号段 → None（走关键词归类）。
    """
    s = title.strip()
    m = re.match(r"^\d{1,3}[-–](\d{1,3})(?:[、.．-]|$)", s)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d{1,3})[、.．]", s)
    if m:
        return int(m.group(1))
    return None


def group_of(title: str) -> str:
    num = course_number(title)
    if num is not None:
        for lo, hi, name in SERIES:
            if lo <= num <= hi:
                return name
    low = title.lower()
    for kws, name in KEYWORDS:
        if any(k in low for k in kws):
            return name
    return DEFAULT_GROUP


def _course_blocks(d: Path, limit: int = 60) -> list[str]:
    """每门课的块级文本：逐知识点（标题+概念摘要），最多 limit 块。"""
    try:
        secs = json.loads((d / "sections.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out: list[str] = []
    for sec in secs:
        for kp in sec.get("knowledge_points", []):
            t = (kp.get("title", "") + " " +
                 (kp.get("concept") or kp.get("detail", "")))[:400]
            if t.strip():
                out.append(t)
            if len(out) >= limit:
                return out
    return out


def content_clusters(courses: list[Path], embedder, threshold: float = 0.68,
                  top_k: int = 5) -> list[list[Path]]:
    """按课程内容的块级相似度贪心聚类（平均连接）。

    度量：每门课逐知识点嵌入为块向量集合；两课相似度 = 双向 top-k 块相似度
    均值（A 的每块取与 B 的最相似块、取最高的 k 个平均，B→A 同理再平均）。
    块级度量避免整课单向量把局部强相关信号平均掉——实测同系列 p50 0.697、
    跨系列 0.630，分离度明显好于整课质心（0.855/0.825）与块级均值（0.475/0.453）。

    无知识块的课程不参与聚类（嵌入字面量会彼此高度相似而错误抱团），单独成组。
    """
    import numpy as np

    blocks = {d: _course_blocks(d) for d in courses}
    vecs: dict[Path, "np.ndarray"] = {}
    todo = [d for d in courses if blocks[d]]
    for d in todo:
        e = np.asarray(embedder.embed(blocks[d]), dtype=np.float32)
        e /= (np.linalg.norm(e, axis=1, keepdims=True) + 1e-9)
        vecs[d] = e
    print(f"  嵌入 {len(vecs)} 门课的知识块完成", flush=True)

    def sim(a: Path, b: Path) -> float:
        M = vecs[a] @ vecs[b].T          # (na, nb) 块对相似度矩阵
        k = min(top_k, M.shape[0], M.shape[1])
        top_a = np.sort(M.max(axis=1))[-k:].mean()
        top_b = np.sort(M.max(axis=0))[-k:].mean()
        return float((top_a + top_b) / 2)

    clusters: list[list[Path]] = []
    for d in courses:
        if d not in vecs:                # 无知识块：单独成组
            clusters.append([d])
            continue
        best, best_s = None, threshold
        for cl in clusters:
            members = [m for m in cl if m in vecs]
            if not members:
                continue
            s = sum(sim(d, m) for m in members) / len(members)
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
        # 排序键：课程编号（优先第二编号段），无编号排最后按日期
        order = course_number(title or "")
        if order is None:
            m = re.search(r"(\d{1,3})", title or d.name)
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


# 分段生成：组内课程超过该数量就分批，每批单独生成再拼接。
# 单次调用输出上限（max_tokens）有限，大组（45 门）一次性写完必被截断——
# 实测 3 组曾断在自检清单中途，故按批次拆分。批大小同时受本地 16k 上下文约束。
_CHUNK_COURSES = 8
_CTX_CHARS = 12000

_SECTIONS_FULL = (
    "## 课程构成\n(列出组内课程及各自定位, 一句话)\n"
    "## 知识体系全景图\n(文字树状图, 跨课程组织)\n"
    "## 学习主线\n(3-6 步主线, 说明先后依赖)\n"
    "## 核心知识点速览\n(按主题分组列出要点, 标注来自哪门课)\n"
    "## 易混淆点对照表\n| 概念A | 概念B | 区别 | 记忆方法 |\n"
    "## 跨课程学习顺序建议\n(编号路径, 标注可并行项)\n"
    "## 综合自检清单\n(逐知识点检查项)\n"
)

_CHUNK_RULE = (
    "纪律：只使用【课程素材】中出现的内容；补充标注「（补充，非课程内容）」。\n"
    "**输出必须完整**：自检清单要写完最后一项，不要中途停止或省略。\n"
    "{part}\n\n"
)


def _gen_once(llm, group_name: str, ctx: str, part: str = "",
              max_tokens: int = 8192) -> str:
    hint = (f"本次只覆盖素材中的部分课程，标题写「{group_name} · 整合复习"
            f"（{part}）」。\n" if part else "")
    system = (
        "你是双擎学习助手的跨课程整合模块。以下是同一课程体系下的多门相关课程"
        "的知识点摘要。生成一份**整合复习文档**（Markdown），结构：\n"
        f"# {group_name} · 整合复习\n"
        + _SECTIONS_FULL
        + _CHUNK_RULE.format(part=hint)
        + f"【课程素材】\n{ctx}")
    return llm.chat(system, "请生成整合复习文档。", max_tokens=max_tokens)


def gen_integration(llm, group_name: str, courses: list[Path]) -> str:
    """生成整合复习文档；大组分批生成后拼接，避免单次输出被截断。"""
    if len(courses) <= _CHUNK_COURSES:
        return _gen_once(llm, group_name, truncate(group_context(courses), _CTX_CHARS))
    batches = [courses[i:i + _CHUNK_COURSES]
               for i in range(0, len(courses), _CHUNK_COURSES)]
    docs: list[str] = []
    for i, batch in enumerate(batches, 1):
        ctx = truncate(group_context(batch), _CTX_CHARS)
        docs.append(_gen_once(llm, group_name, ctx,
                              part=f"{i}/{len(batches)}", max_tokens=8192))
        print(f"  分段 {i}/{len(batches)} 完成（{len(batch)} 门课）", flush=True)
    # 拼接：首段保留大标题与「课程构成」，后续段各自成节
    merged = [docs[0].rstrip()]
    for d in docs[1:]:
        body = d.strip()
        if body.startswith("# "):                       # 去掉重复大标题
            body = body.split("\n", 1)[1].lstrip() if "\n" in body else ""
        merged.append("\n\n---\n\n" + body)
    full = "\n".join(merged) + "\n"
    # 分段提示只服务于生成过程，成品标题不带「（i/n）」
    lines = full.split("\n", 1)
    if lines[0].lstrip("# ").strip() != f"{group_name} · 整合复习":
        lines[0] = f"# {group_name} · 整合复习"
        full = "\n".join(lines)
    return full


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="课程整合：相关课程聚合生成整合复习")
    ap.add_argument("--output", default="output",
                    help="课程产物根目录（默认 output，子目录含 sections.json 即算一门课）")
    ap.add_argument("--dest", default="output/课程整合")
    ap.add_argument("--only", default="", help="只处理指定组名（调试用）")
    ap.add_argument("--by-title", action="store_true",
                    help="仅按标题编号分组（不用内容聚类）")
    ap.add_argument("--threshold", type=float, default=0.68,
                    help="内容聚类的块级相似度阈值（默认 0.68）")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已存在的整合复习.md（默认跳过）")
    args = ap.parse_args(argv)

    cfg = load_config()
    local_chain, cloud_chain = build_client_chains(cfg)
    from course_agent.llm import Router
    # 本地 GPU 优先：远程链故障窗（DNS/超时）会让每次调用白等数分钟，
    # 连续失败 1 次即跳过该模型；云端只做本地失败兜底（同 supervisor 策略）
    llm = Router(local_chain + cloud_chain, skip_threshold=1)

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
        if md.is_file() and md.stat().st_size > 500 and not args.force:
            print(f"[{name}] 已存在，跳过", flush=True)
            continue
        # 组内课程清单随文档落盘，webui 据此展示成员
        (dest / "组.json").write_text(json.dumps(
            {"name": name, "courses": [d.name for d in courses]},
            ensure_ascii=False, indent=1), encoding="utf-8")
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
