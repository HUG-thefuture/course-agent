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
    "fill_blank": "填空", "short_answer": "简答",
}


def _w(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write(text)


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
            lines.append("")
            answers.append(f"**{no}.** {e.answer}")
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
    d = Path(outdir)
    d.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}

    def put(name: str, text: str) -> None:
        p = d / name
        _w(p, text)
        files[name] = str(p)

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
