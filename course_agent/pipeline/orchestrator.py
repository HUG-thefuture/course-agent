"""「编排」阶段：思考 -> 计划 -> 审查 -> 执行 -> 测试 的闭环。

忠实实现需求中的工作流：
- 审查不通过 -> 带审查意见回到「思考」重新计划；
- 测试不通过 -> 带测试问题回到「思考」整体重做；
- 外层循环有上限（max_attempts），保证必然终止，不会无限循环。
"""

from __future__ import annotations

import json
import logging
import time

from ..config import AgentConfig
from ..llm.base import BaseLLM
from ..memory import Memory
from ..pipeline.models import (AttemptRecord, Material, SessionResult)
from ..prompts import render
from ..utils import slugify, truncate
from .executor import ExecuteError, generate_section
from .planner import PlanError, make_plan, validate_plan_structure
from .reviewer import review_plan
from .tester import run_qa

_log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, llm: BaseLLM, cfg: AgentConfig,
                 review_llm: BaseLLM | None = None,
                 memory: Memory | None = None,
                 vision_llm: BaseLLM | None = None) -> None:
        self.llm = llm
        self.review_llm = review_llm or llm
        self.cfg = cfg
        self.memory = memory
        self.vision_llm = vision_llm

    def _extract_frames(self, session_id: str, material: Material) -> list:
        """视频素材提取关键帧；视觉模型可用时生成屏幕演示描述。失败只降级。"""
        from pathlib import Path
        try:
            from ..ingest.frames import extract_keyframes
            out_dir = Path(self.cfg.data_dir) / "frames" / session_id
            frames = extract_keyframes(material.source, out_dir,
                                       max_frames=self.cfg.pipeline.max_frames)
        except Exception as e:
            _log.warning("关键帧提取失败（视觉通道跳过）: %s", e)
            return []
        if self.vision_llm is not None:
            from ..llm.vision import describe_frames
            frames = describe_frames(self.vision_llm, frames)
            n = sum(1 for f in frames if f.caption)
            _log.info("屏幕演示描述完成：%d/%d 帧", n, len(frames))
        return frames

    def _captions_for_section(self, frames: list, sections: list, idx: int,
                              chunks) -> str:
        """取该章节时间范围内的屏幕演示记录文本。"""
        if not frames:
            return ""
        from ..llm.vision import captions_for_range
        sec = sections[idx]
        start = next((chunks[i].start_time for i in sec.chunk_indices
                      if chunks[i].start_time is not None), None)
        end = None
        if idx + 1 < len(sections):
            end = next((chunks[i].start_time for i in sections[idx + 1].chunk_indices
                        if chunks[i].start_time is not None), None)
        return captions_for_range(frames, start, end)

    def run(self, material: Material, title: str | None = None) -> SessionResult:
        from ..ingest.loader import chunk_material
        pcfg = self.cfg.pipeline
        course_title = (title or material.title or "课程").strip()
        chunks = chunk_material(material, pcfg.chunk_chars)
        session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{slugify(course_title)[:24]}"
        partial = material.total_chars < pcfg.partial_chars
        partial_note = (
            "材料可能只是完整课程的一部分（片段/节选），请按现有内容组织，"
            "不要假设或编造未包含的部分。" if partial else "材料完整。")
        _log.info("素材「%s」共 %d 字，分为 %d 块%s", course_title,
                  material.total_chars, len(chunks),
                  "（按不完整课程处理）" if partial else "")

        # ---- 视频素材：提取屏幕关键帧，视觉模型可用时生成屏幕演示记录 ----
        frames: list = []
        if "video" in material.kind and pcfg.extract_frames:
            frames = self._extract_frames(session_id, material)

        feedback = ""
        attempts: list[AttemptRecord] = []
        plan = None
        sections: list = []
        qa = None

        for attempt_no in range(1, pcfg.max_attempts + 1):
            rec = AttemptRecord(index=attempt_no)
            _log.info("=== 第 %d/%d 轮（思考 -> 计划 -> 审查 -> 执行 -> 测试）===",
                      attempt_no, pcfg.max_attempts)

            # ---- 思考 ----
            think = self.llm.chat(
                render(
                    "think", title=course_title, chunk_count=len(chunks),
                    total_chars=material.total_chars,
                    has_timestamps="true" if material.has_timestamps else "false",
                    partial_note=partial_note,
                    feedback=feedback or "（首轮，无）",
                ),
                "请开始分析。",
            )
            _log.info("思考：%s", truncate(think.replace("\n", " "), 120))

            # ---- 计划 ----
            try:
                plan = make_plan(self.llm, course_title, chunks, feedback, pcfg,
                                 partial_note=partial_note)
            except PlanError as e:
                rec.review_issues = [str(e)]
                attempts.append(rec)
                feedback = f"计划生成失败：{e}。请调整策略后重新输出计划。"
                _log.error("%s", feedback)
                continue
            _log.info("计划通过：%d 个章节（%s）", len(plan.sections),
                      "、".join(truncate(s.title, 16) for s in plan.sections))

            # ---- 审查计划 ----
            code_issues = validate_plan_structure(plan, chunks, pcfg)
            review = review_plan(self.review_llm, plan, chunks, code_issues)
            rec.review_passed = review.passed
            rec.review_issues = list(review.issues)
            if not review.passed:
                attempts.append(rec)
                feedback = "计划审查未通过，问题清单：\n- " + "\n- ".join(review.issues)
                _log.warning("计划审查未通过（%d 个问题），返回思考阶段重试",
                             len(review.issues))
                continue
            _log.info("计划审查通过")

            # ---- 执行 ----
            sections = []
            try:
                for si, psec in enumerate(plan.sections):
                    _log.info("生成章节：%s", psec.title)
                    caps = self._captions_for_section(
                        frames, plan.sections, si, chunks)
                    sections.append(generate_section(
                        self.llm, course_title, psec, chunks, pcfg, self.memory,
                        frame_captions=caps))
            except ExecuteError as e:
                rec.qa_issues = [str(e)]
                attempts.append(rec)
                feedback = f"执行阶段失败：{e}。请反思原因并重新规划。"
                _log.error("%s", feedback)
                continue

            # ---- 测试 ----
            qa = run_qa(self.review_llm, plan, sections, pcfg)
            rec.qa_passed = qa.passed
            rec.qa_issues = list(qa.issues)
            attempts.append(rec)
            if qa.passed:
                _log.info("测试通过，得分 %d（忠实度 %.0f%%）",
                          qa.score, qa.fidelity_ratio * 100)
                break
            feedback = "测试未通过，问题清单：\n- " + "\n- ".join(qa.issues[:12])
            _log.warning("测试未通过（得分 %d），返回思考阶段重试", qa.score)

        passed = bool(qa and qa.passed)
        result = SessionResult(
            session_id=session_id, title=course_title,
            source=material.source, plan=plan or _empty_plan(course_title),
            sections=sections, qa=qa, attempts=attempts,
            passed=passed, frames=frames, partial=partial)
        if passed and self.memory is not None and qa.score >= pcfg.exemplar_min_score:
            for sec in sections:
                self.memory.add_example(
                    session_id=session_id, section_title=sec.title,
                    source_excerpt=sec.source_excerpt,
                    output_json=json.dumps(sec.to_dict(), ensure_ascii=False),
                    score=float(qa.score))
            self.memory.save_session(session_id, course_title, material.source,
                                     1 if passed else 0, float(qa.score))
            _log.info("已沉淀 %d 个章节范例到记忆库", len(sections))
        elif self.memory is not None:
            self.memory.save_session(session_id, course_title, material.source,
                                     1 if passed else 0,
                                     float(qa.score) if qa else 0.0)
        return result


def _empty_plan(title: str):
    from ..pipeline.models import Plan
    return Plan(course_title=title)
