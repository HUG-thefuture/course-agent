"""「审查计划」阶段：代码硬校验 + 模型语义审查，双保险。"""

from __future__ import annotations

import logging

from ..llm.base import BaseLLM
from ..pipeline.models import Chunk, Plan, Review
from ..prompts import render
from ..utils import extract_json, truncate

_log = logging.getLogger(__name__)


def review_plan(llm: BaseLLM, plan: Plan, chunks: list[Chunk],
                code_issues: list[str]) -> Review:
    """先跑代码校验结果（调用方传入），再让模型做语义审查。

    模型审查输出异常时重试一次；仍失败则只依赖代码校验（不因审查员
    自身故障卡死流程），并记录警告。
    """
    issues = list(code_issues)
    llm_passed = True
    import json as _json
    plan_json = _json.dumps(
        {"course_title": plan.course_title,
         "sections": [{"title": s.title, "goals": s.goals,
                       "chunk_indices": s.chunk_indices,
                       "exercise_count": s.exercise_count}
                      for s in plan.sections]},
        ensure_ascii=False)
    previews = "\n".join(
        f"#{c.index}: {truncate(c.text.replace(chr(10), ' '), 80)}" for c in chunks)

    last_err = ""
    for attempt in range(2):
        prompt = render("review", plan_json=plan_json, chunk_previews=previews)
        if last_err:
            prompt += f"\n【注意】你上一次的输出无法解析（{last_err}），请严格按 JSON 格式重新输出。"
        try:
            raw = llm.chat(prompt, "请只输出审查结论 JSON。", task="review")
            data = extract_json(raw)
            if not isinstance(data, dict) or "passed" not in data:
                raise ValueError("缺少 passed 字段")
            llm_passed = bool(data["passed"])
            for issue in data.get("issues") or []:
                if str(issue).strip():
                    issues.append(str(issue).strip())
            break
        except (ValueError, TypeError) as e:
            last_err = str(e)
            _log.warning("审查输出解析失败（第 %d 次）: %s", attempt + 1, e)
    else:
        _log.warning("模型审查连续失败，仅依据代码校验结论放行/驳回")

    passed = not code_issues and llm_passed
    return Review(passed=passed, issues=issues)
