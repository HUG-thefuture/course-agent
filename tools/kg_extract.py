"""知识图谱关系抽取：用大模型从课程知识块中抽取知识点与前置/关联关系。

从 data/kb_index.json（BM25 索引）读取全部知识块，按课程分组，
逐课程让大模型抽取知识点关系（prerequisite/related_to/confused_with），
写入 data/kg.json。支持断点续跑（--force 重抽）。

用法（后台运行）：python tools/kg_extract.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_agent.config import load_config  # noqa: E402
from course_agent.kg import KnowledgeGraph  # noqa: E402
from course_agent.kb_vector import extract_keywords  # noqa: E402
from course_agent.llm import OpenAICompatClient, Router  # noqa: E402
from course_agent.llm.factory import build_client_chains  # noqa: E402
from course_agent.utils import similarity, truncate  # noqa: E402


def resolve_title(title_to_id: dict, title: str) -> str | None:
    """精确匹配优先，退化到相似度匹配（阈值 0.35）。"""
    t = truncate(title, 40)
    if t in title_to_id:
        return title_to_id[t]
    best, best_s = None, 0.0
    for k in title_to_id:
        s = similarity(t, k)
        if s > best_s:
            best_s, best = s, k
    return title_to_id[best] if best_s >= 0.35 else None


def extract_course_relations(llm, course: str, blocks: list[dict]) -> list[dict]:
    titles = "\n".join(f"- {b['section']} :: {b['title'][:50]}"
                       for b in blocks[:40])
    prompt = (
        "你是课程知识图谱构建器。下面是一门课的知识块清单（章节::标题）。"
        "识别其中的知识点及其关系，只输出 JSON：\n"
        '{"relations": [{"src": "知识点A", "dst": "知识点B", '
        '"rel": "prerequisite|related_to|confused_with"}]}\n'
        "约束：\n"
        "- prerequisite 必须严格成立（不学A无法理解B），宁缺毋滥；\n"
        "- src/dst 使用清单中出现的原始措辞；\n"
        "- 最多 15 条，只输出最有把握的；\n"
        "- 相同主题不同章节的关系用 related_to。\n\n"
        f"【课程】{course}\n【知识块清单】\n{titles}")
    raw = llm.chat(prompt, "请只输出 JSON。", max_tokens=1536)
    from course_agent.utils import extract_json
    data = extract_json(raw)
    return [r for r in data.get("relations", []) if isinstance(r, dict)
            and r.get("src") and r.get("dst") and r.get("rel") in
            ("prerequisite", "related_to", "confused_with")]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="知识图谱关系抽取")
    ap.add_argument("--index", default="data/kb_index.json")
    ap.add_argument("--out", default="data/kg.json")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    cfg = load_config()
    local_chain, cloud_chain = build_client_chains(cfg)
    llm = Router(cloud_chain + local_chain)

    idx = json.loads(Path(args.index).read_text(encoding="utf-8"))
    chunks = idx.get("chunks", [])
    by_course: dict[str, list[dict]] = {}
    for c in chunks:
        by_course.setdefault(c.get("course", "未命名"), []).append(c)

    courses = sorted(by_course)
    if args.limit:
        courses = courses[: args.limit]

    kg = KnowledgeGraph(Path(args.out))
    kg.load()

    # 节点：每个知识块注册一个知识点
    for c in chunks:
        kg.add_node(c.get("id", c.get("title", "")),
                    c.get("title", "")[:40],
                    course=c.get("course", "")[:40],
                    section=c.get("section", "")[:40],
                    difficulty=2, bloom="理解")
    title_to_id: dict[str, str] = {}
    for c in chunks:
        title_to_id[c.get("title", "")[:40]] = c.get("id", "")

    done_n = fail_n = edges = 0
    for ci, course in enumerate(courses, 1):
        blocks = by_course[course]
        try:
            rels = extract_course_relations(llm, course, blocks)
            for r in rels:
                src_id = resolve_title(title_to_id, r["src"])
                dst_id = resolve_title(title_to_id, r["dst"])
                if src_id and dst_id:
                    kg.add_edge(src_id, dst_id, r["rel"])
                    edges += 1
            done_n += 1
            print(f"[{ci}/{len(courses)}] {course[:30]} -> {len(rels)} 条关系",
                  flush=True)
        except Exception as e:
            fail_n += 1
            print(f"[{ci}/{len(courses)}] {course[:30]} 失败: {truncate(str(e), 80)}",
                  flush=True)
        time.sleep(0.5)

    kg.save()
    print(f"\n抽取完成：成功 {done_n}，失败 {fail_n}，关系边 {edges} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
