"""为存量课程重新生成编程实战题（升级后带在线测试断言）。

背景：早期批次的编程题题干可能被截断、且无在线测试断言。本工具按原章节
边界用升级后的生成逻辑重出各章编程题（保留单选/判断等其他题型），
更新 sections.json 与 exercises.md。已有合格编程题的章节自动跳过。

用法（后台运行）：
  python tools/regen_exercises.py --output "output/特训营全部课程" \
      --transcripts data/harvest_transcripts
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
from course_agent.ingest.loader import chunk_material, load_source  # noqa: E402
from course_agent.llm import OpenAICompatClient  # noqa: E402
from course_agent.pipeline.executor import _generate_coding  # noqa: E402
from course_agent.pipeline.models import (Exercise, SectionOutput,  # noqa: E402
                                          KnowledgePoint, KeyNode)
from course_agent.pipeline.tester import validate_exercise  # noqa: E402
from course_agent.outputs import _exercises_md  # noqa: E402
from course_agent.utils import truncate  # noqa: E402


def exercises_ok(sec: dict) -> bool:
    """该节已有合格编程题（带测试断言且题干完整）则跳过。"""
    for e in sec.get("exercises", []):
        if e.get("type") != "coding_practice":
            continue
        errs = validate_exercise(e)
        if not errs and e.get("tests"):
            return True
    return False


def regen_course(course_dir: Path, transcript_path: Path, llm, pcfg) -> str:
    plan = json.loads((course_dir / "plan.json").read_text(encoding="utf-8"))
    course_title = plan.get("course_title") or course_dir.name
    sections = json.loads((course_dir / "sections.json").read_text(encoding="utf-8"))
    material = load_source(str(transcript_path))
    chunks = chunk_material(material, 3000)

    changed = 0
    rebuilt: list[SectionOutput] = []
    for si, sec in enumerate(sections):
        idx = sec.get("chunk_indices") or []
        text = "\n\n".join(chunks[i].text for i in idx if i < len(chunks))
        # 重建 SectionOutput（供 exercises.md 渲染）
        out = SectionOutput(
            title=sec.get("title", ""),
            goals=sec.get("goals", []),
            knowledge_points=[KnowledgePoint(**{**{"title": "", "detail": ""}, **k})
                              for k in sec.get("knowledge_points", [])],
            key_nodes=[KeyNode(**{**{"label": "", "why": ""}, **n})
                       for n in sec.get("key_nodes", [])],
            source_text=text,
        )

        if exercises_ok(sec):
            coding = [Exercise(**{**{"type": "coding_practice", "question": "",
                                     "answer": ""}, **e})
                      for e in sec.get("exercises", [])
                      if e.get("type") == "coding_practice"]
            out.exercises = [Exercise(**e) for e in sec.get("exercises", [])]
            rebuilt.append(out)
            continue

        psec = type("P", (), {"title": sec.get("title", ""),
                              "chunk_indices": idx,
                              "exercise_count": pcfg.min_exercises})()
        try:
            coding = _generate_coding(llm, course_title, sec.get("title", ""),
                                      text, pcfg)
        except Exception as e:
            print(f"    编程题生成失败（保留原样）: {truncate(str(e), 80)}", flush=True)
            coding = []

        others = [Exercise(**e) for e in sec.get("exercises", [])
                  if e.get("type") != "coding_practice"]
        out.exercises = others + coding
        rebuilt.append(out)
        changed += len(coding)

    # 校验全部习题，剔除不合格
    for out in rebuilt:
        keep = []
        for e in out.exercises:
            errs = validate_exercise(e.to_dict())
            if errs:
                print(f"    剔除不合格习题: {truncate(e.question, 30)} ({errs[0]})",
                      flush=True)
                continue
            keep.append(e)
        out.exercises = keep

    # 写回
    secs_json = []
    for src, out in zip(sections, rebuilt):
        d = src.copy()
        d["exercises"] = [e.to_dict() for e in out.exercises]
        secs_json.append(d)
    (course_dir / "sections.json").write_text(
        json.dumps(secs_json, ensure_ascii=False, indent=2), encoding="utf-8")

    fake = type("R", (), {"title": course_title, "sections": rebuilt})()
    (course_dir / "exercises.md").write_text(_exercises_md(fake), encoding="utf-8")
    total = sum(len(o.exercises) for o in rebuilt)
    return f"{len(rebuilt)} 章 {total} 题（本次新出编程题 {changed}）"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="存量课程编程题重生成")
    ap.add_argument("--output", default="output/特训营全部课程")
    ap.add_argument("--transcripts", default="data/harvest_transcripts")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="已有合格编程题也重出")
    args = ap.parse_args(argv)

    cfg = load_config()
    pcfg = cfg.pipeline
    llm = OpenAICompatClient(base_url=cfg.local.base_url, model=cfg.local.model,
                             api_key=cfg.local.api_key, timeout=cfg.local.timeout,
                             max_retries=cfg.local.max_retries)

    root = Path(args.output)
    todo = []
    for d in sorted(root.iterdir()):
        if not (d.is_dir() and (d / "plan.json").is_file()
                and (d / "sections.json").is_file()):
            continue
        stem = re.sub(r"^\d{2,3}-", "", d.name)
        transcript = Path(args.transcripts) / f"{stem}.json"
        if not transcript.is_file():
            continue
        if args.force or not exercises_ok_first(d):
            todo.append((d, transcript))
    if args.limit:
        todo = todo[: args.limit]
    print(f"待重出编程题 {len(todo)} 门", flush=True)

    ok = fail = 0
    for i, (d, t) in enumerate(todo, 1):
        try:
            info = regen_course(d, t, llm, pcfg)
            print(f"[{i}/{len(todo)}] {d.name[:40]} -> {info}", flush=True)
            ok += 1
        except Exception as e:
            print(f"[{i}/{len(todo)}] 失败: {truncate(str(e), 100)}", flush=True)
            fail += 1
        time.sleep(1)
    print(f"\n编程题重生成完成：成功 {ok}，失败 {fail}")
    return 0


def exercises_ok_first(d: Path) -> bool:
    """粗筛：整门课所有编程题都已有 tests 且题干完整才跳过。"""
    try:
        secs = json.loads((d / "sections.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True
    for sec in secs:
        for e in sec.get("exercises", []):
            if e.get("type") == "coding_practice":
                errs = validate_exercise(e)
                if errs or not e.get("tests"):
                    return False
    return True


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
