"""为已完成的课程重新生成「复习笔记 review.md」（升级后的知识点结构）。

背景：早期批次的笔记是"讲课实录+时间戳"导向；升级后的提示词产出
概念讲解+代码示例+易错点+相关性拓展。本工具用新提示词对存量课程
按原有章节边界重跑笔记生成（不重新出题），把 review.md 写入课程目录。

用法（后台运行，逐门处理，断点续跑——已有 review.md 的自动跳过）：
  python tools/regen_review.py --output "output/特训营全部课程" \
      --transcripts data/harvest_transcripts
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
from course_agent.ingest.loader import chunk_material, load_source  # noqa: E402
from course_agent.llm import OpenAICompatClient, Router  # noqa: E402
from course_agent.pipeline.executor import _parse_notes  # noqa: E402
from course_agent.prompts import render  # noqa: E402
from course_agent.utils import extract_json, fmt_time, truncate  # noqa: E402


def _review_md(course_title: str, sections: list[dict]) -> str:
    """用解析出的笔记数据拼装复习笔记 markdown（与 outputs._review_md 同风格）。"""
    lines = [f"# {course_title} · 复习笔记", "",
             "> 本文目标：**不回看视频也能复习掌握**。每个知识点含概念讲解、示例、易错点；",
             "> 文末的「相关拓展」是课程内容之外的关联知识补充，已明确标注。", ""]
    for sec in sections:
        lines.append(f"## {sec['title']}")
        lines.append("")
        kps = sec.get("_parsed_kps") or []
        expansions = sec.get("_parsed_expansions") or []
        if not kps:
            lines.append("（本章笔记生成失败，可 --force 重跑）")
            lines.append("")
            continue
        for i, kp in enumerate(kps, 1):
            ts = f"（课程 {kp.time} 前后）" if kp.time else ""
            lines.append(f"### {i}. {kp.title}{ts}")
            lines.append("")
            lines.append(f"**概念讲解**：{kp.concept or kp.detail}")
            lines.append("")
            if kp.example:
                lines.append("**示例**：")
                lines.append("```")
                lines.append(kp.example.strip())
                lines.append("```")
                lines.append("")
            if kp.pitfall:
                lines.append(f"**⚠️ 易错点**：{kp.pitfall}")
                lines.append("")
        if expansions:
            lines.append(f"### 📌 相关拓展（课程外补充，共 {len(expansions)} 条）")
            lines.append("")
            for e in expansions:
                lines.append(f"- **{e.topic}**：{e.content}")
            lines.append("")
        lines.append("---")
        lines.append("")
    lines.append("*拓展内容基于模型知识补充，学习时以课程与官方文档为准。*")
    return "\n".join(lines)


def regen_course(course_dir: Path, transcript_path: Path, llm, max_retries: int = 1) -> str:
    plan = json.loads((course_dir / "plan.json").read_text(encoding="utf-8"))
    course_title = plan.get("course_title") or course_dir.name
    material = load_source(str(transcript_path))
    chunks = chunk_material(material, 3000)

    sections_out = []
    for sec in plan.get("sections", []):
        idx = sec.get("chunk_indices") or []
        text = "\n\n".join(chunks[i].text for i in idx if i < len(chunks))
        has_ts = any(chunks[i].start_time is not None for i in idx if i < len(chunks))
        start_raw = next((chunks[i].start_time for i in idx
                          if i < len(chunks) and chunks[i].start_time is not None), None)
        anchors = "\n".join(
            f"#{i}: {fmt_time(chunks[i].start_time)}"
            for i in idx if i < len(chunks) and chunks[i].start_time is not None)
        kps = nodes = expansions = None
        last = ""
        for attempt in range(max_retries + 1):
            try:
                prompt = render(
                    "notes", title=course_title, section_title=sec.get("title", ""),
                    has_timestamps="true" if has_ts else "false",
                    start_time=fmt_time(start_raw) or "无",
                    time_anchors=anchors or "无", source_text=text,
                    exemplars="（暂无）")
                raw = llm.chat(prompt, "请只输出 JSON。")
                kps, nodes, expansions = _parse_notes(extract_json(raw))
                break
            except (ValueError, TypeError) as e:
                last = str(e)
        if kps is None:
            print(f"    章节失败: {truncate(sec.get('title', ''), 24)} ({last})", flush=True)
            kps, nodes, expansions = [], [], []
        sections_out.append({"title": sec.get("title", ""), "_parsed_kps": kps,
                             "_parsed_nodes": nodes, "_parsed_expansions": expansions,
                             "_chunk_count": len(idx)})

    md = _review_md(course_title, sections_out)
    (course_dir / "review.md").write_text(md, encoding="utf-8")
    total_kps = sum(len(s["_parsed_kps"]) for s in sections_out)
    total_exp = sum(len(s["_parsed_expansions"]) for s in sections_out)
    return f"{len(sections_out)} 章 {total_kps} 知识点 {total_exp} 拓展"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="存量课程复习笔记重生成")
    ap.add_argument("--output", default="output/特训营全部课程")
    ap.add_argument("--transcripts", default="data/harvest_transcripts")
    ap.add_argument("--force", action="store_true", help="已有 review.md 也重生成")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    cfg = load_config()
    from course_agent.llm import MockLLM  # noqa: F401
    local = OpenAICompatClient(
        base_url=cfg.local.base_url, model=cfg.local.model,
        api_key=cfg.local.api_key, timeout=cfg.local.timeout,
        max_retries=cfg.local.max_retries)
    clients = [local]
    if cfg.api.enabled and cfg.api_key_effective:
        clients.append(OpenAICompatClient(
            base_url=cfg.api.base_url, model=cfg.api.model,
            api_key=cfg.api_key_effective, timeout=cfg.api.timeout,
            max_retries=cfg.api.max_retries))
    llm = Router(clients)

    root = Path(args.output)
    courses = sorted(d for d in root.iterdir()
                     if d.is_dir() and (d / "plan.json").is_file())
    todo = []
    for d in courses:
        rv = d / "review.md"
        if args.force or not (rv.is_file() and rv.stat().st_size > 200):
            todo.append(d)
    if args.limit:
        todo = todo[: args.limit]
    print(f"待重生成 {len(todo)}/{len(courses)} 门", flush=True)

    ok = fail = 0
    for i, d in enumerate(todo, 1):
        stem = d.name.split("-", 1)[-1] if re_match_prefix(d.name) else d.name
        transcript = Path(args.transcripts) / f"{stem}.json"
        if not transcript.is_file():
            print(f"[{i}/{len(todo)}] 找不到转写稿 {transcript.name}，跳过", flush=True)
            fail += 1
            continue
        try:
            info = regen_course(d, transcript, llm)
            print(f"[{i}/{len(todo)}] {d.name[:40]} -> {info}", flush=True)
            ok += 1
        except Exception as e:
            print(f"[{i}/{len(todo)}] 失败: {truncate(str(e), 120)}", flush=True)
            fail += 1
        time.sleep(1)
    print(f"\n复习笔记重生成完成：成功 {ok}，失败/跳过 {fail}")
    return 0


def re_match_prefix(name: str) -> bool:
    import re
    return bool(re.match(r"^\d{2,3}-", name))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
