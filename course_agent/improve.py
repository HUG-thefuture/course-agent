"""自改进（训练）模块。

诚实说明：本模块不做大模型权重微调（本地硬件成本高、结果难复现），
而是通过两条已验证有效的路径实现"越用越好"：
1. 范例沉淀 + 相似度召回：质检高分产物自动成为少样本范例，
   后续生成同主题内容时注入提示词，显著提升格式与内容稳定性；
2. 评估循环：在样例材料上多轮运行并记录得分曲线，量化改进效果。

如需真正的权重训练，`export_sft` 可把范例库导出为 Alpaca 格式 JSONL，
直接用于 LLaMA-Factory / PEFT 等 LoRA 微调框架。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from .config import AgentConfig
from .ingest.loader import load_source
from .llm.base import BaseLLM
from .memory import Memory
from .pipeline.orchestrator import Orchestrator

_log = logging.getLogger(__name__)


def run_improvement(llm: BaseLLM, cfg: AgentConfig, memory: Memory,
                    inputs: list[tuple[str, str]], rounds: int = 2,
                    review_llm: Optional[BaseLLM] = None) -> dict:
    """在给定材料上跑多轮 生成->质检->沉淀范例 的改进循环。

    inputs: [(素材路径, 课程标题), ...]
    返回统计摘要；每轮明细追加写入 data/metrics.jsonl。
    """
    if rounds < 1:
        raise ValueError("rounds 至少为 1")
    metrics_path = Path(cfg.data_dir) / "metrics.jsonl"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    for r in range(1, rounds + 1):
        for src, title in inputs:
            material = load_source(src)
            orch = Orchestrator(llm, cfg, review_llm=review_llm, memory=memory)
            result = orch.run(material, title=title)
            rec = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "round": r,
                "input": str(src),
                "title": title,
                "session_id": result.session_id,
                "passed": result.passed,
                "score": result.qa.score if result.qa else 0,
                "exercises": sum(len(s.exercises) for s in result.sections),
                "attempts": len(result.attempts),
            }
            records.append(rec)
            with metrics_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            _log.info("改进轮 %d/%d · %s · 得分 %d", r, rounds, title, rec["score"])

    summary = {"rounds": rounds, "runs": len(records),
               "metrics_file": str(metrics_path)}
    if records:
        scores = [r["score"] for r in records]
        summary["avg_score"] = round(sum(scores) / len(scores), 1)
        first_round = [r["score"] for r in records if r["round"] == 1]
        last_round = [r["score"] for r in records if r["round"] == rounds]
        summary["first_round_avg"] = round(sum(first_round) / len(first_round), 1) if first_round else 0
        summary["last_round_avg"] = round(sum(last_round) / len(last_round), 1) if last_round else 0
        summary["examples_bank"] = memory.count_examples()
    return summary


def export_sft(memory: Memory, out_path: str | Path) -> int:
    """把范例库导出为 Alpaca 格式 JSONL，供 LoRA 微调使用。"""
    rows = memory.all_examples()
    if not rows:
        raise ValueError("范例库为空：先运行 run/improve 沉淀高质量范例")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    instruction = ("根据以下直播课程片段，输出 JSON：知识点列表、关键节点列表、"
                   "配套习题（含答案与解析）。要求忠于原文、格式严格。")
    n = 0
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({
                "instruction": instruction,
                "input": r["source_excerpt"],
                "output": r["output_json"],
            }, ensure_ascii=False) + "\n")
            n += 1
    _log.info("已导出 %d 条 SFT 样本到 %s", n, out)
    return n
