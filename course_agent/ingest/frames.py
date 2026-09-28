"""视频关键帧提取：用 ffmpeg 场景检测抓"屏幕内容发生变化"的帧。

用途：编程题需要知道屏幕上演示了什么代码/操作。未配置视觉模型时，
帧也会存档到产物目录，供学习者做题时对照回看。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from ..pipeline.models import Frame

_log = logging.getLogger(__name__)

_PTS_RE = re.compile(r"pts_time:([\d.]+)")


class FrameError(Exception):
    pass


def _run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    if shutil.which("ffmpeg") is None:
        raise FrameError("未找到 ffmpeg，无法提取视频帧（安装 ffmpeg 或关闭 extract_frames）")
    return subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "info",
                           *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)


def extract_keyframes(video: str | Path, out_dir: str | Path,
                      max_frames: int = 24,
                      scene_threshold: float = 0.3) -> list[Frame]:
    """提取关键帧。优先场景检测；检不出（如静态画面）时按 60s 间隔采样兜底。

    返回按时间排序的 Frame 列表（不超过 max_frames，均匀抽稀）。
    """
    video = Path(video)
    if not video.is_file():
        raise FrameError(f"视频不存在: {video}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("frame_*.jpg"):
        old.unlink(missing_ok=True)

    vf = f"select='gt(scene,{scene_threshold})',showinfo"
    proc = _run_ffmpeg(["-i", str(video), "-vf", vf, "-vsync", "vfr",
                        "-frames:v", str(max_frames),
                        str(out / "frame_%04d.jpg")])
    times = sorted(float(m.group(1)) for m in _PTS_RE.finditer(proc.stderr or ""))
    files = sorted(out.glob("frame_*.jpg"))

    if not files:
        # 场景检测无命中（画面基本静态）→ 逐级放宽采样间隔兜底
        for fps in ("1/60", "1/10", "1/2"):
            _log.info("场景检测无命中，改用 %s 秒间隔采样兜底", fps)
            proc = _run_ffmpeg(["-i", str(video), "-vf", f"fps={fps},showinfo",
                                "-frames:v", str(max_frames),
                                str(out / "frame_%04d.jpg")])
            times = sorted(float(m.group(1))
                           for m in _PTS_RE.finditer(proc.stderr or ""))
            files = sorted(out.glob("frame_*.jpg"))
            if files:
                break

    if not files:
        raise FrameError("未能从视频中提取任何帧")

    # showinfo 时间可能少于文件数（解析差异），缺省按序号 60s 间隔补齐
    frames = []
    for i, f in enumerate(files):
        t = times[i] if i < len(times) else i * 60.0
        frames.append(Frame(path=str(f), time=t))

    frames.sort(key=lambda x: x.time)
    if len(frames) > max_frames:  # 均匀抽稀
        step = len(frames) / max_frames
        frames = [frames[int(i * step)] for i in range(max_frames)]
    _log.info("提取关键帧 %d 张 -> %s", len(frames), out)
    return frames
