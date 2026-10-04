"""视频直链批量下载器：持续把 data/video_urls 里的新鲜直链下载为本地 mp4。

与浏览器采集流水线配合：采集器逐门产出直链 json，本脚本轮询下载（选更小的
speaker 流），全部就绪后退出。下载完的视频由 tools/video_transcribe.py 转写。

用法：
  python tools/download_videos.py --courses data/video_courses.json \
      --urls data/video_urls --videos data/videos --expect 34
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path


_BROWSWER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"),
    "Referer": "https://meeting.tencent.com/",
}


def download(url: str, dest: Path) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=_BROWSWER_HEADERS)
    with urllib.request.urlopen(req, timeout=120) as r, part.open("wb") as f:
        while True:
            chunk = r.read(1024 * 512)
            if not chunk:
                break
            f.write(chunk)
    if part.stat().st_size < 1_000_000:
        size = part.stat().st_size
        part.unlink(missing_ok=True)
        raise RuntimeError(f"下载内容过小({size}B)，token 可能已过期")
    part.rename(dest)


def pick(urls: list[str]) -> str:
    urls = sorted(urls, key=lambda u: (0 if "_speaker" in u else 1, len(u)))
    return urls[0]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="视频直链批量下载器")
    ap.add_argument("--courses", default="data/video_courses.json")
    ap.add_argument("--urls", default="data/video_urls")
    ap.add_argument("--videos", default="data/videos")
    ap.add_argument("--expect", type=int, default=34)
    ap.add_argument("--max-hours", type=float, default=6.0)
    ap.add_argument("--poll", type=int, default=60, help="轮询间隔秒数")
    args = ap.parse_args(argv)

    courses = json.loads(Path(args.courses).read_text(encoding="utf-8"))
    url_dir, vid_dir = Path(args.urls), Path(args.videos)
    vid_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.max_hours * 3600

    stats = {"ok": 0, "fail": 0}
    fails: dict[int, int] = {}  # 连续失败次数，达到上限才放弃
    while time.time() < deadline:
        pending = []
        for c in courses:
            mp4 = vid_dir / f"{c['seq']:03d}.mp4"
            if mp4.is_file():
                continue
            uf = url_dir / f"{c['seq']:03d}.json"
            if not uf.is_file():
                pending.append(c)
                continue
            urls = json.loads(uf.read_text(encoding="utf-8")).get("urls") or []
            if not urls:
                pending.append(c)
                continue
            if fails.get(c["seq"], 0) >= 60:
                continue  # 多次失败（多半 token 已过期等待重采），本轮放弃
            try:
                download(pick(urls), mp4)
                stats["ok"] += 1
                fails.pop(c["seq"], None)
                print(f"[{c['seq']:03d}] 下载完成（{mp4.stat().st_size // 1048576} MB）",
                      flush=True)
            except Exception as e:
                fails[c["seq"]] = fails.get(c["seq"], 0) + 1
                print(f"[{c['seq']:03d}] 下载失败（第 {fails[c['seq']]} 次）：{e}", flush=True)
                if fails[c["seq"]] < 60:
                    pending.append(c)  # 留在待办里，等重采后重试
        if not pending:
            print(f"\n全部就绪：成功 {stats['ok']}，失败 {stats['fail']}")
            return 0
        print(f"等待：还差 {len(pending)} 门，{args.poll} 秒后重试", flush=True)
        time.sleep(args.poll)
    print("超时退出")
    return 1


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))
