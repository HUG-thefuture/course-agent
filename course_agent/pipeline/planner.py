"""「计划」阶段：让模型把文本块组织成章节计划，并做严格的结构校验与有限重试。"""

from __future__ import annotations

import logging

from ..config import PipelineConfig
from ..llm.base import BaseLLM
from ..pipeline.models import Chunk, Plan, PlanSection
from ..prompts import render
from ..utils import extract_json, truncate

_log = logging.getLogger(__name__)

MAX_INTERNAL_TRIES = 3
MAX_SECTIONS = 8


class PlanError(Exception):
    """计划生成失败（重试后仍不合法）。"""


def validate_plan_structure(plan: Plan, chunks: list[Chunk],
                            pcfg: PipelineConfig) -> list[str]:
    """代码级硬校验：覆盖完整性是硬约束，不可静默修复。"""
    issues: list[str] = []
    if not plan.sections:
        return ["计划中没有任何章节"]
    if len(plan.sections) > MAX_SECTIONS:
        issues.append(f"章节数 {len(plan.sections)} 超过上限 {MAX_SECTIONS}")
    n = len(chunks)
    seen: list[int] = []
    for i, sec in enumerate(plan.sections):
        if not sec.title.strip():
            issues.append(f"第{i + 1}节标题为空")
        if not sec.chunk_indices:
            issues.append(f"第{i + 1}节「{truncate(sec.title, 20)}」未分配任何文本块")
            continue
        for idx in sec.chunk_indices:
            if not isinstance(idx, int) or not (0 <= idx < n):
                issues.append(f"第{i + 1}节引用了不存在的文本块编号 {idx!r}")
            else:
                seen.append(idx)
    dup = {x for x in seen if seen.count(x) > 1}
    if dup:
        issues.append(f"文本块被重复分配: {sorted(dup)}")
    missing = sorted(set(range(n)) - set(seen))
    if missing:
        issues.append(f"文本块未被任何章节覆盖: {missing}")
    return issues


def _normalize(plan_data: dict, pcfg: PipelineConfig) -> Plan:
    """宽容归一化：类型修正、exercise_count 夹取。结构性错误交给校验。"""
    if not isinstance(plan_data, dict):
        raise ValueError("计划必须是 JSON 对象")
    sections_raw = plan_data.get("sections")
    if not isinstance(sections_raw, list) or not sections_raw:
        raise ValueError("sections 必须是非空数组")
    plan = Plan(course_title=str(plan_data.get("course_title", "")).strip() or "课程")
    for s in sections_raw:
        if not isinstance(s, dict):
            raise ValueError("sections 元素必须是对象")
        raw_idx = s.get("chunk_indices", [])
        if not isinstance(raw_idx, list):
            raise ValueError("chunk_indices 必须是数组")
        idx: list[int] = []
        for x in raw_idx:
            if isinstance(x, bool):
                raise ValueError("chunk_indices 不能是布尔值")
            idx.append(int(x))
        count = s.get("exercise_count", 4)
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = pcfg.min_exercises
        count = max(pcfg.min_exercises, min(pcfg.max_exercises, count))
        goals = [str(g) for g in (s.get("goals") or []) if str(g).strip()]
        plan.sections.append(PlanSection(
            title=str(s.get("title", "")).strip(),
            goals=goals,
            chunk_indices=idx,
            exercise_count=count,
        ))
    return plan


def make_plan(llm: BaseLLM, title: str, chunks: list[Chunk], feedback: str,
              pcfg: PipelineConfig, partial_note: str = "材料完整。") -> Plan:
    """生成计划：内部最多重试 MAX_INTERNAL_TRIES 次，错误信息回灌。"""
    previews = "\n".join(
        f"#{c.index} (起始 {('第%d秒' % c.start_time) if c.start_time is not None else '无时间戳'}): "
        f"{truncate(c.text.replace(chr(10), ' '), 80)}"
        for c in chunks
    )
    base_feedback = feedback.strip()
    last_err = ""
    for attempt in range(1, MAX_INTERNAL_TRIES + 1):
        fb = base_feedback + (f"\n【上一次计划未通过的原因】{last_err}" if last_err else "")
        prompt = render(
            "plan",
            title=title,
            chunk_count=len(chunks),
            chunk_count_minus=len(chunks) - 1,
            min_exercises=pcfg.min_exercises,
            max_exercises=pcfg.max_exercises,
            chunk_previews=previews,
            partial_note=partial_note,
            feedback=fb,
        )
        raw = llm.chat(prompt, "请只输出计划 JSON。", task="plan")
        try:
            data = extract_json(raw)
            plan = _normalize(data, pcfg)
            issues = validate_plan_structure(plan, chunks, pcfg)
            if not issues:
                return plan
            last_err = "；".join(issues)
        except (ValueError, TypeError) as e:
            last_err = f"输出解析失败: {e}"
        _log.warning("计划第 %d 次尝试未通过: %s", attempt, last_err)
    raise PlanError(f"计划生成连续 {MAX_INTERNAL_TRIES} 次失败: {last_err}")
