"""音视频转写：依赖 faster-whisper（已随项目验证安装），本地 CPU 运行。

模型默认 small（中文效果与体积均衡，约 460MB，已预取到本地缓存）。
可用环境变量 COURSE_AGENT_WHISPER_MODEL 覆盖（tiny/base/small/medium/large-v3）。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from ..pipeline.models import Segment

# 国内网络默认走 HF 镜像下载模型；用户已设置 HF_ENDPOINT 时以用户为准
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

_log = logging.getLogger(__name__)


class TranscribeError(Exception):
    pass


def transcribe_media(path: str | Path, model_size: str | None = None) -> list[Segment]:
    """本地转写音/视频为带时间戳的 Segment。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise TranscribeError(
            "音视频转写需要 faster-whisper：pip install faster-whisper\n"
            "  （CPU 即可运行；也可以改用字幕/文稿输入绕过转写）") from e
    size = model_size or os.environ.get("COURSE_AGENT_WHISPER_MODEL", "small")
    beam = int(os.environ.get("COURSE_AGENT_WHISPER_BEAM", "5"))
    t0 = time.time()
    try:
        model = WhisperModel(size, device="cpu", compute_type="int8")
        segments_iter, info = model.transcribe(str(path), vad_filter=True,
                                               language=None, beam_size=beam)
        segments = [Segment(start=seg.start, text=seg.text.strip())
                    for seg in segments_iter if seg.text.strip()]
    except Exception as e:
        raise TranscribeError(f"转写失败: {e}") from e
    finally:
        # 显式释放模型权重，批处理多个文件时控制内存峰值
        model = None  # noqa: F841
    _log.info("转写完成：%s，语言=%s，共 %d 段，耗时 %.0f 秒",
              Path(path).name, getattr(info, "language", "?"),
              len(segments), time.time() - t0)
    return segments


def fmt_srt_time(seconds: float) -> str:
    """转 SRT 时间戳：HH:MM:SS,mmm。"""
    total = max(0.0, seconds)
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = int(total % 60)
    ms = int(round((total - int(total)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments) -> str:
    blocks = []
    for i, seg in enumerate(segments, 1):
        start = seg.start if seg.start is not None else 0.0
        end = start + max(1.5, len(seg.text) * 0.18)  # 粗略时长估计，仅供回看定位
        blocks.append(f"{i}\n{fmt_srt_time(start)} --> {fmt_srt_time(end)}\n{seg.text}\n")
    return "\n".join(blocks)
