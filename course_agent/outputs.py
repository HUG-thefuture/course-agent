"""产物输出：知识点笔记 / 关键节点 / 习题（题目与答案分离）/ JSON / 质检报告。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .pipeline.models import SessionResult
from .utils import truncate

_log = logging.getLogger(__name__)

_TYPE_LABEL = {
    "single_choice": "单选", "multi_choice": "多选", "true_false": "判断",
    "fill_blank": "填空", "short_answer": "简答", "coding_practice": "编程实战",
}


def _code_block(code: str) -> str:
    if not code:
        return ""
    return f"\n```\n{code.strip()}\n```\n"


def _w(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write(text)


def _review_md(result: SessionResult) -> str:
    """复习笔记（主文档）：概念+示例+易错点+相关性拓展，独立于视频可复习。"""
    lines = [f"# {result.title} · 复习笔记", "",
             "> 本文目标：**不回看视频也能复习掌握**。每个知识点含概念讲解、示例、易错点；",
             "> 文末的「相关拓展」是课程内容之外的关联知识补充，已明确标注。", ""]
    for sec in result.sections:
        lines.append(f"## {sec.title}")
        if sec.goals:
            lines.append(f"*学习目标：{'；'.join(sec.goals)}*")
        lines.append("")
        if not sec.knowledge_points:
            lines.append("（本章无知识点）")
        for i, kp in enumerate(sec.knowledge_points, 1):
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
        if sec.expansions:
            lines.append(f"### 📌 相关拓展（课程外补充，共 {len(sec.expansions)} 条）")
            lines.append("")
            for e in sec.expansions:
                lines.append(f"- **{e.topic}**：{e.content}")
            lines.append("")
        lines.append("---")
        lines.append("")
    lines.append("*拓展内容基于模型知识补充，学习时以课程与官方文档为准。*")
    return "\n".join(lines)


def _notes_md(result: SessionResult) -> str:
    lines = [f"# {result.title} · 知识点笔记", "",
             f"> 来源：{result.source}  ", f"> 会话：{result.session_id}  ",
             f"> 质检得分：{result.qa.score if result.qa else '-'}", ""]
    for sec in result.sections:
        lines.append(f"## {sec.title}")
        if sec.goals:
            lines.append("**学习目标**：" + "；".join(sec.goals))
        lines.append("")
        lines.append("### 知识点")
        for kp in sec.knowledge_points:
            ts = f" `[{kp.time}]`" if kp.time else ""
            lines.append(f"- **{kp.title}**{ts}：{kp.detail}")
        lines.append("")
        if sec.key_nodes:
            lines.append("### 关键节点")
            lines.append("| 时间 | 节点 | 为什么重要 |")
            lines.append("| --- | --- | --- |")
            for n in sec.key_nodes:
                lines.append(f"| {n.time or '-'} | {n.label} | {n.why or '-'} |")
            lines.append("")
    return "\n".join(lines)


def _key_nodes_md(result: SessionResult) -> str:
    lines = [f"# {result.title} · 关键节点速查", "",
             "复习时优先回看这些节点：", ""]
    for sec in result.sections:
        if not sec.key_nodes:
            continue
        lines.append(f"## {sec.title}")
        for n in sec.key_nodes:
            ts = f"[{n.time}] " if n.time else ""
            lines.append(f"- {ts}**{n.label}** —— {n.why}")
        lines.append("")
    return "\n".join(lines)


def _exercises_md(result: SessionResult) -> str:
    lines = [f"# {result.title} · 配套习题", "",
             "先作答，再对照文末答案。", ""]
    answers = [""]
    no = 0
    for sec in result.sections:
        lines.append(f"## {sec.title}")
        lines.append("")
        for e in sec.exercises:
            no += 1
            tag = _TYPE_LABEL.get(e.type, e.type)
            lines.append(f"**{no}. [{tag}·{e.difficulty}]** {e.question}")
            if e.options:
                for letter, opt in zip("ABCDEFGH", e.options):
                    lines.append(f"   - {letter}. {opt}")
            if e.type == "coding_practice":
                if e.starter_code:
                    lines.append("")
                    lines.append("   **起始代码：**")
                    lines.append(_code_block(e.starter_code))
                if e.hints:
                    lines.append("   **提示：** " + "；".join(e.hints))
            lines.append("")
            answers.append(f"**{no}.**")
            if e.type == "coding_practice":
                answers.append(_code_block(e.answer))
                if e.check_points:
                    answers.append("   **验收要点：**")
                    for cp in e.check_points:
                        answers.append(f"   - [ ] {cp}")
            else:
                answers.append(f"   {e.answer}")
            if e.explanation:
                answers.append(f"   解析：{e.explanation}")
        lines.append("")
    lines.append("---")
    lines.append("# 参考答案")
    lines.extend(answers)
    return "\n".join(lines)


def _report_md(result: SessionResult) -> str:
    lines = [f"# 质检报告 · {result.session_id}", ""]
    final = "通过" if result.passed else "未通过"
    lines.append(f"- 最终结论：**{final}**（得分 {result.qa.score if result.qa else 0}）")
    if result.qa:
        lines.append(f"- 忠实度抽查：{result.qa.fidelity_ratio:.0%}")
    lines.append(f"- 迭代轮数：{len(result.attempts)}")
    lines.append("")
    for rec in result.attempts:
        lines.append(f"## 第 {rec.index} 轮")
        lines.append(f"- 计划审查：{'通过' if rec.review_passed else '未通过'}")
        for i in rec.review_issues:
            lines.append(f"  - 审查问题：{truncate(i, 160)}")
        lines.append(f"- 产物测试：{'通过' if rec.qa_passed else '未通过'}")
        for i in rec.qa_issues[:10]:
            lines.append(f"  - 测试问题：{truncate(i, 160)}")
        lines.append("")
    return "\n".join(lines)


def write_all(result: SessionResult, outdir: str | Path) -> dict[str, str]:
    """写出全部产物，返回文件名 -> 路径。目录不存在则创建。"""
    import shutil

    d = Path(outdir)
    d.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}

    def put(name: str, text: str) -> None:
        p = d / name
        _w(p, text)
        files[name] = str(p)

    # 视频关键帧存档（若有），供做题时对照回看
    if result.frames:
        frames_dir = d / "frames"
        frames_dir.mkdir(exist_ok=True)
        for f in result.frames:
            src = Path(f.path)
            if src.is_file():
                shutil.copy2(src, frames_dir / src.name)
        files["frames/"] = str(frames_dir)

    put("review.md", _review_md(result))
    put("notes.md", _notes_md(result))
    put("key_nodes.md", _key_nodes_md(result))
    put("exercises.md", _exercises_md(result))
    put("review_report.md", _report_md(result))
    put("plan.json", json.dumps(
        {"course_title": result.plan.course_title,
         "sections": [{"title": s.title, "goals": s.goals,
                       "chunk_indices": s.chunk_indices,
                       "exercise_count": s.exercise_count}
                      for s in result.plan.sections]},
        ensure_ascii=False, indent=2))
    put("sections.json", json.dumps(
        [s.to_dict() for s in result.sections], ensure_ascii=False, indent=2))
    summary: dict[str, Any] = result.to_summary()
    summary["qa_issues"] = result.qa.issues if result.qa else []
    summary["attempts_detail"] = [
        {"index": r.index, "review_passed": r.review_passed,
         "review_issues": r.review_issues, "qa_passed": r.qa_passed,
         "qa_issues": r.qa_issues} for r in result.attempts]
    put("session.json", json.dumps(summary, ensure_ascii=False, indent=2))
    _log.info("产物已写入 %s", d)
    return files
