"""提示词加载：course_agent/prompts/*.md，{{var}} 占位符替换。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

_ALLOWED = {"think", "plan", "review", "notes", "exercises", "qa_judge",
            "coding_practice"}


class PromptError(Exception):
    pass


def render(name: str, **kwargs: Any) -> str:
    """加载模板并替换 {{var}}；未提供的占位符替换为空串。

    单趟正则替换：注入的变量值不会被再次扫描——课程素材里合法出现的
    {{message}}（Vue 插值）、f"{{}}"（f-string 转义）等内容原样保留。
    """
    if name not in _ALLOWED:
        raise PromptError(f"未知提示词模板: {name}")
    path = _PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptError(f"提示词文件缺失: {path}")
    text = path.read_text(encoding="utf-8")

    def _sub(m: re.Match) -> str:
        k = m.group(1)
        if k in kwargs and kwargs[k] is not None:
            return str(kwargs[k])
        return ""  # 未提供的占位符

    return re.sub(r"\{\{(\w+)\}\}", _sub, text)


def task_marker(rendered: str) -> str:
    """提取模板首行的任务标记（供 Mock 模型与日志使用）。"""
    m = re.search(r"TASK:\s*(\w+)", rendered or "")
    return m.group(1) if m else "UNKNOWN"
