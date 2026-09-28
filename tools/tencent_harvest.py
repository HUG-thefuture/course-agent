"""腾讯会议录制清单批量采集器：纯 HTTP 拉取全部课程的 AI 转写逐字稿。

已验证的接口链（无需浏览器/Cookie，2026-09-28）：
  1. GET /cw/<short_link>                  -> 页面 HTML 内嵌 share UUID
  2. GET get-multi-record-info(share,pwd)  -> recording_id / 时长 / 文件列表
  3. GET minutes/detail(id,pwd,recording_id, limit=1000) -> 逐字稿 JSON

输出：每门课一个 CourseAgent 管线可直接使用的转写 JSON（loader json 格式），
外加 report.json 汇总。已存在的输出自动跳过（断点续跑，--force 重采）。

用法：
  python tools/tencent_harvest.py data/catalog_courses.json --outdir data/harvest_transcripts
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
UA = {"User-Agent": "Mozilla/5.0 (CourseAgent harvest/1.0)"}


def _get(url: str, timeout: int = 30, retries: int = 2) -> str:
    last: Exception | None = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # 网络抖动重试
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败 {url[:80]}: {last}")


def resolve_share(short: str) -> str:
    html = _get(f"https://meeting.tencent.com/cw/{short}")
    m = UUID_RE.search(html)
    if not m:
        raise RuntimeError("页面未内嵌 share id（可能不是录制分享页）")
    return m.group(0)


def get_recording_id(share: str, pwd: str) -> tuple[str, float]:
    ts = int(time.time() * 1000)
    body = _get(
        "https://meeting.tencent.com/wemeet-tapi/v2/meetlog/public/record-detail/"
        f"get-multi-record-info?c_timestamp={ts}&c_nonce=h{ts % 100000}"
        "&platform=Web&c_app_uid=&c_district=0&c_lang=zh-CN&mock=1"
        f"&pwd={pwd}&auth_share_id={share}&uni_record_share_id={share}&activity_uid=")
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError(f"record-info code={data.get('code')} {data.get('msg', '')}")
    base = (data.get("data") or {}).get("base_infos") or []
    if not base:
        raise RuntimeError("record-info 无录制文件")
    return base[0]["recording_id"], int(base[0].get("duration", 0)) / 60000.0


def get_minutes(share: str, pwd: str, recording_id: str,
                split_version: int = 1) -> dict:
    ts = int(time.time() * 1000)
    body = _get(
        "https://meeting.tencent.com/wemeet-cloudrecording-webapi/v1/minutes/detail"
        f"?c_timestamp={ts}&c_nonce=t{ts % 100000}&platform=Web&c_app_uid=&c_district=0"
        f"&mock=1&id={share}&pwd={pwd}&activity_uid=&page_source=record"
        f"&meeting_id=&recording_id={recording_id}"
        "&lang=zh&minutes_version=0&limit=1000&return_ori=0&start_pid=0"
        f"&return_ori_minutes_translating=1&split_version={split_version}&fview=0")
    return json.loads(body)


def minutes_to_transcript(data: dict) -> list[dict]:
    """minutes JSON -> CourseAgent loader json 格式（start 秒 + text）。"""
    paras = (data.get("minutes") or {}).get("paragraphs") or []
    rows = []
    for p in paras:
        texts = [w.get("text", "") for s in p.get("sentences", [])
                 for w in s.get("words", [])]
        text = "".join(t for t in texts if t)
        if text.strip():
            rows.append({"start": round((p.get("start_time") or 0) / 1000.0, 2),
                         "text": text})
    return rows


def harvest_one(course: dict) -> dict:
    short = course["source"].rstrip("/").rsplit("/", 1)[-1]
    share = resolve_share(short)
    recording_id, duration_min = get_recording_id(share, course["password"])
    # 转写管线有两代：新记录用 split_version=1，2026-08 底前的部分记录只有 0
    data: dict = {}
    for sv in (1, 0):
        data = get_minutes(share, course["password"], recording_id, split_version=sv)
        paras = (data.get("minutes") or {}).get("paragraphs") or []
        if data.get("code") == 0 and paras:
            break
    if data.get("code") != 0:
        return {"status": "no_minutes" if data.get("code") in (30001, -1) else "api_error",
                "code": data.get("code"), "duration_min": round(duration_min, 1)}
    rows = minutes_to_transcript(data)
    return {"status": "ok", "paragraphs": len(rows),
            "chars": sum(len(r["text"]) for r in rows),
            "duration_min": round(duration_min, 1), "rows": rows}


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="腾讯会议录制批量采集器")
    ap.add_argument("catalog", help="parse_recording_catalog.py 生成的清单 JSON")
    ap.add_argument("--outdir", default="data/harvest_transcripts")
    ap.add_argument("--delay", type=float, default=1.0, help="每课间隔秒数")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 门（调试用）")
    ap.add_argument("--force", action="store_true", help="忽略已有产物重采")
    args = ap.parse_args(argv[1:])

    courses = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    if args.limit:
        courses = courses[: args.limit]
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)

    stats: dict[str, int] = {}
    report: list[dict] = []
    t0 = time.time()
    for c in courses:
        name = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", c["title"]).strip("-")
        dest = out / f"{name}.json"
        entry = {"seq": c["seq"], "title": c["title"], "source": c["source"]}
        if dest.is_file() and not args.force:
            entry.update(status="ok", cached=True)
            report.append(entry)
            stats["cached"] = stats.get("cached", 0) + 1
            continue
        try:
            r = harvest_one(c)
            entry.update(r)
            if r["status"] == "ok" and r["rows"]:
                dest.write_text(json.dumps(r.pop("rows"), ensure_ascii=False, indent=1),
                                encoding="utf-8")
                entry["file"] = str(dest)
            stats[r["status"]] = stats.get(r["status"], 0) + 1
        except Exception as e:
            entry.update(status="error", error=str(e)[:200])
            stats["error"] = stats.get("error", 0) + 1
        report.append(entry)
        print(f"[{c['seq']:03d}/{len(courses)}] {entry['status']:10s} {c['title']}"
              + (f" ({entry.get('chars', 0)} 字)" if entry.get("status") == "ok" else "")
              + (f" err={entry.get('error', '')}" if entry.get("status") == "error" else ""),
              flush=True)
        time.sleep(args.delay)

    (out / "harvest_report.json").write_text(
        json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "stats": stats, "elapsed_min": round((time.time() - t0) / 60, 1),
                    "courses": report}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"\n采集完成 {stats}，用时 {round((time.time()-t0)/60,1)} 分钟")
    print(f"转写文件在 {out}，可直接: python main.py batch --input {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
