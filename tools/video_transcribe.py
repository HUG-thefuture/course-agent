"""无转写老课的视频下载 + 本地 whisper 转写批量脚本。

输入：data/video_urls/<seq>.json（浏览器采集的 mp4 直链，含时效 token）
输出：data/harvest_transcripts/<标题>.json（CourseAgent 管线格式），转写完自动删视频省盘。

设计：
- 与 89 门课的批量管线串行执行（机器 16G 内存，whisper 与 qwen3:8b 并行会 OOM）；
- 支持断点续跑：已有转写产物/已删视频但有转写的课程自动跳过；
- 选更小的流（speaker 摄像头流的音频与 screen 相同，体积更小）；
- 可 --wait-all 等待全部直链文件就绪（配合浏览器采集尚未完成时串在队列里）。

用法：
  python tools/video_transcribe.py --courses data/video_courses.json \
      --urls data/video_urls --out data/harvest_transcripts \
      --wait-all --expect 34 --max-wait-hours 12
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _slug(title: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "-", title).strip("-")


def _download(url: str, dest: Path) -> None:
    """流式下载到 .part，完成后改名（断电不留半个好文件）。

    COS 防盗链校验 Referer/UA，必须带浏览器同款头，否则 403。
    """
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"),
        "Referer": "https://meeting.tencent.com/"})
    with urllib.request.urlopen(req, timeout=120) as r, part.open("wb") as f:
        while True:
            chunk = r.read(1024 * 512)
            if not chunk:
                break
            f.write(chunk)
    size = part.stat().st_size
    if size < 1_000_000:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"下载内容过小({size}B)，token 可能已过期")
    part.rename(dest)


def _transcribe(path: Path) -> list[dict]:
    # 走项目内转写器（模型/beam 可用环境变量覆盖），beam=1 提速约 1.5-2 倍
    os.environ.setdefault("COURSE_AGENT_WHISPER_BEAM", "1")
    import sys
    sys.path.insert(0, str(ROOT))
    from course_agent.ingest.transcriber import transcribe_media
    segs = transcribe_media(path)
    return [{"start": s.start, "text": s.text} for s in segs if s.text.strip()]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="无转写老课：视频下载+本地转写")
    ap.add_argument("--courses", default="data/video_courses.json")
    ap.add_argument("--urls", default="data/video_urls")
    ap.add_argument("--out", default="data/harvest_transcripts")
    ap.add_argument("--videos", default="data/videos", help="视频临时目录")
    ap.add_argument("--wait-all", action="store_true", help="等待全部直链文件就绪")
    ap.add_argument("--expect", type=int, default=0, help="期望的直链文件总数")
    ap.add_argument("--max-wait-hours", type=float, default=12.0)
    args = ap.parse_args(argv)

    courses = json.loads(Path(args.courses).read_text(encoding="utf-8"))
    url_dir = Path(args.urls)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    video_dir = Path(args.videos)
    video_dir.mkdir(parents=True, exist_ok=True)

    # --wait-all：轮询等浏览器采集完成
    if args.wait_all and args.expect:
        deadline = time.time() + args.max_wait_hours * 3600
        while time.time() < deadline:
            done = sum(1 for c in courses
                       if (url_dir / f"{c['seq']:03d}.json").is_file()
                       and json.loads((url_dir / f"{c['seq']:03d}.json")
                                      .read_text(encoding="utf-8")).get("urls"))
            if done >= args.expect:
                print(f"直链已全部就绪 {done}/{args.expect}")
                break
            print(f"等待浏览器采集直链 {done}/{args.expect}，30 分钟后再查", flush=True)
            time.sleep(1800)
        else:
            print("等待超时，按当前已就绪的直链继续", flush=True)

    ok = fail = 0
    for c in courses:
        seq = c["seq"]
        name = _slug(c["title"])
        dest = out_dir / f"{name}.json"
        if dest.is_file():  # 已转写
            ok += 1
            continue
        url_file = url_dir / f"{seq:03d}.json"
        if not url_file.is_file():
            print(f"[{seq:03d}] 无直链文件，跳过", flush=True)
            fail += 1
            continue
        entry = json.loads(url_file.read_text(encoding="utf-8"))
        urls = entry.get("urls") or []
        if not urls:
            print(f"[{seq:03d}] 直链为空，跳过（待补采）", flush=True)
            fail += 1
            continue
        # 选更小的流：_speaker 体积通常小于 _screen，音频一致
        urls.sort(key=lambda u: (0 if "_speaker" in u else 1, len(u)))
        mp4 = video_dir / f"{seq:03d}.mp4"
        try:
            if not mp4.is_file():
                print(f"[{seq:03d}] 下载 {entry['title']}", flush=True)
                _download(urls[0], mp4)
            print(f"[{seq:03d}] 转写中（约需数十分钟）…", flush=True)
            rows = _transcribe(mp4)
            if not rows:
                raise RuntimeError("转写结果为空")
            dest.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                            encoding="utf-8")
            mp4.unlink(missing_ok=True)  # 省磁盘
            chars = sum(len(r["text"]) for r in rows)
            print(f"[{seq:03d}] 完成：{len(rows)} 段 {chars} 字 -> {dest.name}", flush=True)
            ok += 1
        except Exception as e:
            print(f"[{seq:03d}] 失败：{e}", flush=True)
            fail += 1

    print(f"\n视频转写批次结束：成功 {ok}，失败/跳过 {fail}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
