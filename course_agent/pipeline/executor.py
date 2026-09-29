"""「执行」阶段：按计划逐节生成知识点、关键节点与习题。"""

from __future__ import annotations

import json
import logging

from ..llm.base import BaseLLM
from ..memory import Memory
from ..pipeline.models import Chunk, KeyNode, KnowledgePoint, PlanSection, SectionOutput
from ..prompts import render
from ..utils import extract_json, fmt_time, similarity, truncate
from .tester import exercise_from_dict, validate_exercise

_log = logging.getLogger(__name__)

MAX_PARSE_TRIES = 2


class ExecuteError(Exception):
    pass


def _exemplar_block(memory: Memory | None, query: str) -> str:
    if memory is None:
        return "（暂无）"
    rows = memory.recall_examples(query, k=2)
    if not rows:
        return "（暂无）"
    blocks = []
    for r in rows:
        blocks.append(f"示例片段：{truncate(r['source_excerpt'], 160)}\n期望输出结构："
                      f"{truncate(r['output_json'], 400)}")
    return "\n---\n".join(blocks)


def _render_notes_retry(extra_err: str) -> str:
    return f"【修正要求】你上一次的输出不符合要求：{extra_err}，请严格按 JSON 格式重新输出。" if extra_err else ""


def _parse_notes(data) -> tuple[list[KnowledgePoint], list[KeyNode], list]:
    if not isinstance(data, dict):
        raise ValueError("输出必须是 JSON 对象")
    kps_raw = data.get("knowledge_points")
    nodes_raw = data.get("key_nodes")
    if not isinstance(kps_raw, list) or not kps_raw:
        raise ValueError("knowledge_points 必须是非空数组")
    kps: list[KnowledgePoint] = []
    for item in kps_raw[:12]:
        if not isinstance(item, dict):
            continue
        detail = str(item.get("detail", "")).strip()
        concept = str(item.get("concept", "")).strip()
        title = str(item.get("title", "")).strip()
        # 兼容新旧两种输出：concept 优先，退回 detail
        body = concept or detail
        if not body:
            continue
        kps.append(KnowledgePoint(
            title=title or truncate(body, 16),
            detail=detail or truncate(concept, 120),
            time=str(item.get("time", "") or ""),
            concept=body,
            example=str(item.get("example", "") or ""),
            pitfall=str(item.get("pitfall", "") or ""),
        ))
    if not kps:
        raise ValueError("未解析出有效知识点")
    nodes: list[KeyNode] = []
    for item in (nodes_raw or [])[:6]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        why = str(item.get("why", "")).strip()
        if label:
            nodes.append(KeyNode(label=label, why=why,
                                 time=str(item.get("time", "") or "")))
    # 相关性拓展（课程外补充，文档中明确标注）
    from ..pipeline.models import Expansion
    expansions: list = []
    for item in (data.get("expansions") or [])[:4]:
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic", "")).strip()
        content = str(item.get("content", "")).strip()
        if topic and content:
            expansions.append(Expansion(topic=topic, content=content))
    return kps, nodes, expansions


def _parse_exercises(data) -> tuple[list[Exercise], list[str]]:
    if not isinstance(data, dict) or not isinstance(data.get("exercises"), list):
        raise ValueError("输出必须含 exercises 数组")
    out: list[Exercise] = []
    problems: list[str] = []
    for i, item in enumerate(data["exercises"]):
        if not isinstance(item, dict):
            problems.append(f"第{i}题不是对象")
            continue
        errs = validate_exercise(item)
        if errs:
            problems.append(f"第{i}题: " + "；".join(errs))
            continue
        out.append(exercise_from_dict(item))
    if not out:
        raise ValueError("没有合法习题: " + "；".join(problems[:3]))
    return out, problems


def _generate_coding(llm: BaseLLM, course_title: str, section_title: str,
                     text: str) -> list:
    """生成编程实战题：最多重试 1 次，全部失败抛异常由调用方降级。"""
    last_err = ""
    for attempt in range(2):
        prompt = render("coding_practice", title=course_title,
                        section_title=section_title, source_text=text)
        if last_err:
            prompt += f"\n【修正要求】上一次输出问题：{last_err}，请修正后严格按 JSON 重新输出。"
        try:
            raw = llm.chat(prompt, "请只输出 JSON。", max_tokens=3072)
            coding, problems = _parse_exercises(extract_json(raw))
            if problems:
                _log.info("章节「%s」编程题 %d 道未通过校验被丢弃",
                          truncate(section_title, 20), len(problems))
            return coding[:2]
        except (ValueError, TypeError) as e:
            last_err = str(e)
            _log.warning("编程题生成第 %d 次失败（%s）: %s",
                         attempt + 1, truncate(section_title, 20), e)
    raise ExecuteError(last_err)


def generate_section(llm: BaseLLM, course_title: str, psec: PlanSection,
                     chunks: list[Chunk], pcfg, memory: Memory | None = None,
                     frame_captions: str = "") -> SectionOutput:
    """生成单个章节的完整产物。模型输出异常时带错误信息重试一次。

    frame_captions: 该时间段的屏幕演示记录（视觉模型产出），注入源文本，
    让编程题能"看到"老师课上演示的代码。
    """
    try:
        text = "\n\n".join(chunks[i].text for i in psec.chunk_indices)
    except IndexError as e:
        raise ExecuteError(f"章节「{psec.title}」引用了越界文本块: {e}") from e
    if frame_captions:
        text = text + "\n" + frame_captions
    has_ts = any(chunks[i].start_time is not None for i in psec.chunk_indices)
    start_raw = next((chunks[i].start_time for i in psec.chunk_indices
                      if chunks[i].start_time is not None), None)
    start_time = fmt_time(start_raw)
    anchors = "\n".join(
        f"#{i}: {fmt_time(chunks[i].start_time)}"
        for i in psec.chunk_indices if chunks[i].start_time is not None)
    exemplars = _exemplar_block(memory, psec.title + " " + text[:200])

    # ---- 知识点 + 关键节点 + 相关拓展 ----
    kps: list[KnowledgePoint] = []
    nodes: list[KeyNode] = []
    expansions: list = []
    last_err = ""
    for attempt in range(MAX_PARSE_TRIES):
        prompt = render(
            "notes", title=course_title, section_title=psec.title,
            has_timestamps="true" if has_ts else "false",
            start_time=start_time or "无",
            time_anchors=anchors or "无",
            source_text=text, exemplars=exemplars,
        ) + _render_notes_retry(last_err)
        try:
            raw = llm.chat(prompt, "请只输出 JSON。")
            kps, nodes, expansions = _parse_notes(extract_json(raw))
            break
        except (ValueError, TypeError) as e:
            last_err = str(e)
            _log.warning("知识点生成第 %d 次失败（%s）: %s",
                         attempt + 1, truncate(psec.title, 20), e)
    else:
        raise ExecuteError(f"章节「{psec.title}」知识点生成失败: {last_err}")

    # ---- 习题 ----
    exercises: list[Exercise] = []
    last_err = ""
    for attempt in range(MAX_PARSE_TRIES):
        prompt = render(
            "exercises", title=course_title, section_title=psec.title,
            exercise_count=max(psec.exercise_count, pcfg.min_exercises),
            min_exercises=pcfg.min_exercises, max_exercises=pcfg.max_exercises,
            source_text=text, exemplars=exemplars,
        )
        if last_err:
            prompt += f"\n【修正要求】上一次输出问题：{last_err}，请修正后严格按 JSON 重新输出。"
        try:
            raw = llm.chat(prompt, "请只输出 JSON。")
            exercises, problems = _parse_exercises(extract_json(raw))
            if problems:
                _log.info("章节「%s」有 %d 道题未通过校验被丢弃: %s",
                          truncate(psec.title, 20), len(problems), "；".join(problems[:2]))
            if len(exercises) < pcfg.min_exercises:
                last_err = f"合法习题仅 {len(exercises)} 道（要求 {pcfg.min_exercises} 道），请补足数量"
                continue
            break
        except (ValueError, TypeError) as e:
            last_err = str(e)
            _log.warning("习题生成第 %d 次失败（%s）: %s",
                         attempt + 1, truncate(psec.title, 20), e)
    else:
        raise ExecuteError(f"章节「{psec.title}」习题生成失败: {last_err}")

    # ---- 编程实战题（独立调用，失败不阻断主流程）----
    if getattr(pcfg, "coding_practice", True):
        try:
            coding = _generate_coding(llm, course_title, psec.title, text)
            exercises.extend(coding)
        except Exception as e:  # 增强功能：任何失败都只降级为无编程题
            _log.warning("章节「%s」编程题生成失败（已跳过）: %s",
                         truncate(psec.title, 20), e)

    return SectionOutput(
        title=psec.title, goals=list(psec.goals),
        knowledge_points=kps, key_nodes=nodes, exercises=exercises,
        source_excerpt=text[:600], source_text=text,
        expansions=expansions,
    )
