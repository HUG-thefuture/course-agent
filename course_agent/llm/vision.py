"""屏幕帧视觉描述：把关键帧交给视觉模型，产出"屏幕上演示了什么"的文字记录。

描述结果会按时间归入对应章节，作为编程题的证据来源之一（讲话稿经常漏掉
"屏幕上正在敲的代码"，视觉通道补上这块）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..pipeline.models import Frame
from ..utils import extract_json, fmt_time

_log = logging.getLogger(__name__)

VISION_PROMPT = """<!-- TASK: VISION -->
你是课程屏幕录像分析员。下面按顺序给你若干张课程录屏关键帧（时间点见用户消息）。
请逐张用一句中文描述屏幕上呈现的内容：出现了什么代码/组件/命令/页面/工具，
尽量引用可见的代码关键词（函数名、组件名、命令行）。看不清的写"画面不清晰"。

只输出 JSON：{"frames": [{"idx": 0, "caption": "不超过60字的描述"}, ...]}
idx 与图片顺序一一对应，数量必须与图片数一致。
"""

_MAX_IMAGES_PER_CALL = 6


def describe_frames(llm: Any, frames: list[Frame],
                    max_images_per_call: int = _MAX_IMAGES_PER_CALL) -> list[Frame]:
    """为帧补充 caption。分批调用视觉模型；单批失败则该批无描述（不抛出）。"""
    if not frames:
        return frames
    filled: list[Frame] = []
    for start in range(0, len(frames), max_images_per_call):
        batch = frames[start : start + max_images_per_call]
        times = "、".join(fmt_time(f.time) for f in batch)
        user = f"共 {len(batch)} 张关键帧，按顺序对应时间点：{times}。请逐张描述。"
        try:
            raw = llm.chat_with_image(VISION_PROMPT, user,
                                      [f.path for f in batch])
            data = extract_json(raw)
            rows = data.get("frames") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise ValueError("frames 不是数组")
            captions = {}
            for row in rows:
                if isinstance(row, dict) and "idx" in row:
                    captions[int(row["idx"])] = str(row.get("caption", "")).strip()
            for i, f in enumerate(batch):
                cap = captions.get(i, "")
                filled.append(Frame(path=f.path, time=f.time, caption=cap))
        except Exception as e:
            _log.warning("帧描述失败（该批 %d 张跳过）: %s", len(batch), e)
            filled.extend(Frame(path=f.path, time=f.time) for f in batch)
    return filled


def captions_for_range(frames: Optional[list[Frame]],
                       start: Optional[float], end: Optional[float]) -> str:
    """取落在 [start, end) 时间段内、有描述的帧，拼成屏幕演示记录文本。"""
    if not frames:
        return ""
    rows = []
    for f in frames:
        if f.time is None or not f.caption:
            continue
        if start is not None and f.time < start - 1:
            continue
        if end is not None and f.time >= end + 1:
            continue
        rows.append(f"[{fmt_time(f.time)}] {f.caption}")
    if not rows:
        return ""
    return "\n【屏幕演示记录】\n" + "\n".join(rows)
