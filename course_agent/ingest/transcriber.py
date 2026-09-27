"""音视频转写（可选功能）：依赖 faster-whisper，未安装时给出明确指引。"""

from __future__ import annotations

import logging
from pathlib import Path

from ..pipeline.models import Segment

_log = logging.getLogger(__name__)


class TranscribeError(Exception):
    pass


def transcribe_media(path: str | Path, model_size: str = "small") -> list[Segment]:
    """本地转写音/视频为带时间戳的 Segment。首次使用需下载模型权重。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise TranscribeError(
            "音视频转写需要 faster-whisper：pip install faster-whisper\n"
            "  （CPU 即可运行；也可以改用字幕/文稿输入绕过转写）") from e
    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments_iter, info = model.transcribe(str(path), vad_filter=True,
                                               language=None)
        segments = [Segment(start=seg.start, text=seg.text.strip())
                    for seg in segments_iter if seg.text.strip()]
    except Exception as e:
        raise TranscribeError(f"转写失败: {e}") from e
    finally:
        # 显式释放模型权重，长会话多文件批处理时控制内存峰值
        model = None  # noqa: F841
    _log.info("转写完成：%s，语言=%s，共 %d 段", path, getattr(info, "language", "?"), len(segments))
    return segments
