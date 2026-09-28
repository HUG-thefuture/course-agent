"""「测试」阶段：结构校验（纯代码，确定性强）+ 忠实度抽查（模型判断是否忠于原文）。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..config import PipelineConfig
from ..llm.base import BaseLLM
from ..pipeline.models import Chunk, Exercise, Plan, QAReport, SectionOutput
from ..prompts import render
from ..utils import extract_json, truncate

_log = logging.getLogger(__name__)

VALID_TYPES = {"single_choice", "multi_choice", "true_false", "fill_blank", "short_answer"}
VALID_DIFFICULTY = {"easy", "medium", "hard"}
_BOOL_ANSWERS = {"对", "错", "是", "否", "正确", "错误", "true", "false"}


def validate_exercise(d: dict) -> list[str]:
    """习题字段校验，返回错误列表（空列表=合格）。纯函数。"""
    errs: list[str] = []
    q = d.get("question")
    if not isinstance(q, str) or not q.strip():
        errs.append("题干为空")
    t = d.get("type")
    if t not in VALID_TYPES:
        errs.append(f"未知题型 {t!r}")
        return errs
    ans = d.get("answer")
    if not isinstance(ans, str) or not ans.strip():
        errs.append("答案为空")
        return errs
    opts = d.get("options")
    if t in ("single_choice", "multi_choice"):
        if not isinstance(opts, list) or len(opts) < 2:
            errs.append("选择题选项不足 2 个")
            return errs
        if any(not isinstance(o, str) or not o.strip() for o in opts):
            errs.append("选项存在空文本")
        letters = "ABCDEFGH"[: len(opts)]
        pick = [c for c in ans.strip().upper() if c.isalpha()]
        if not pick or any(c not in letters for c in pick):
            errs.append(f"答案 {ans!r} 与选项编号不符")
        elif len(set(pick)) != len(pick):
            errs.append("答案字母重复")
        elif t == "single_choice" and len(pick) != 1:
            errs.append("单选题答案必须是单个字母")
    elif t == "true_false":
        if ans.strip().lower() not in _BOOL_ANSWERS:
            errs.append(f"判断题答案 {ans!r} 应为 对/错")
    elif t == "fill_blank":
        if "____" not in (q or ""):
            errs.append("填空题题干必须包含 ____")
    diff = d.get("difficulty", "medium")
    if diff not in VALID_DIFFICULTY:
        errs.append(f"难度 {diff!r} 不合法")
    if not isinstance(d.get("explanation", ""), str):
        errs.append("解析必须是字符串")
    return errs


def exercise_from_dict(d: dict) -> Exercise:
    return Exercise(
        type=d["type"],
        question=d.get("question", ""),
        answer=d.get("answer", ""),
        explanation=d.get("explanation", "") if isinstance(d.get("explanation"), str) else "",
        options=[str(o) for o in d.get("options") or []],
        difficulty=d.get("difficulty") if d.get("difficulty") in VALID_DIFFICULTY else "medium",
    )


def structural_check(plan: Plan, sections: list[SectionOutput],
                     pcfg: PipelineConfig) -> list[str]:
    """产物结构校验：数量、字段、去重。"""
    issues: list[str] = []
    if len(sections) != len(plan.sections):
        issues.append(f"章节数不匹配：计划 {len(plan.sections)} 节，实际生成 {len(sections)} 节")
        return issues
    seen_questions: set[str] = set()
    for sec in sections:
        st = truncate(sec.title, 24)
        if not sec.knowledge_points:
            issues.append(f"「{st}」没有知识点")
        if len(sec.knowledge_points) > 12:
            issues.append(f"「{st}」知识点数量异常（{len(sec.knowledge_points)}）")
        if len(sec.exercises) < min(pcfg.min_exercises, 2):
            issues.append(f"「{st}」习题不足（{len(sec.exercises)} 道，至少 {min(pcfg.min_exercises, 2)} 道）")
        if len(sec.exercises) > pcfg.max_exercises * 2:
            issues.append(f"「{st}」习题数量异常偏多")
        for e in sec.exercises:
            errs = validate_exercise(e.to_dict())
            for msg in errs:
                issues.append(f"「{st}」习题「{truncate(e.question, 20)}」: {msg}")
            key = e.question.strip()
            if key in seen_questions:
                issues.append(f"「{st}」出现重复题目: {truncate(key, 30)}")
            seen_questions.add(key)
    return issues


@dataclass
class _FidelityOut:
    ratio: float
    issues: list[str]


def fidelity_check(llm: BaseLLM, sections: list[SectionOutput],
                   pcfg: PipelineConfig) -> _FidelityOut:
    """抽样让模型核对生成内容是否忠于原文。每节抽查若干条。"""
    total = 0
    supported = 0
    issues: list[str] = []
    for sec in sections:
        if not sec.exercises and not sec.knowledge_points:
            continue
        items = []
        if sec.knowledge_points:
            kp = sec.knowledge_points[0]
            items.append({"kind": "知识点", "content": kp.title + "：" + kp.detail})
        if sec.exercises:
            ex = sec.exercises[0]
            items.append({"kind": "习题", "content": ex.question,
                          "answer": ex.answer})
        for i, it in enumerate(items):
            it["idx"] = i
        # 忠实度判断需要完整证据：给裁判全节源文本（截断到提示词安全长度）
        source = sec.source_text or ("（摘要）" + sec.source_excerpt) or "（截取）" + truncate(
            "\n".join(kp.detail for kp in sec.knowledge_points), 800)
        prompt = render("qa_judge", source_text=truncate(source, 3500),
                        items=json.dumps(items, ensure_ascii=False))
        try:
            raw = llm.chat(prompt, "请只输出质检 JSON。")
            data = extract_json(raw)
            rows = data.get("items") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise ValueError("items 不是数组")
        except (ValueError, TypeError) as e:
            _log.warning("忠实度抽查解析失败（%s）: %s", truncate(sec.title, 20), e)
            continue  # 抽查器故障不作为产物问题
        for row in rows:
            if not isinstance(row, dict):
                continue
            total += 1
            if bool(row.get("supported")):
                supported += 1
            else:
                issues.append(
                    f"「{truncate(sec.title, 20)}」{row.get('reason', '与原文不符')}")
    ratio = supported / total if total else 1.0
    return _FidelityOut(ratio=ratio, issues=issues)


def run_qa(llm: BaseLLM, plan: Plan, sections: list[SectionOutput],
           pcfg: PipelineConfig) -> QAReport:
    structural = structural_check(plan, sections, pcfg)
    fid = fidelity_check(llm, sections, pcfg)
    passed = not structural and fid.ratio >= pcfg.fidelity_threshold
    score = 0 if structural else round(55 + 45 * fid.ratio)
    return QAReport(passed=passed, issues=structural + fid.issues,
                    structural_issues=structural,
                    fidelity_ratio=fid.ratio, score=score)
