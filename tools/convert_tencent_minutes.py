"""腾讯会议「纪要逐字稿」JSON -> CourseAgent 管线输入格式转换器。

用法：
  1. 打开回放分享页输入提取码后，F12 -> Network 筛选 `minutes/detail`，
     右键第一条请求 -> Copy -> Copy link address，浏览器直接访问该地址，
     把返回的 JSON 全量保存为 minutes.json（若接口分页，调大 limit 参数）；
  2. python tools/convert_tencent_minutes.py minutes.json transcript.json
  3. python main.py run --input transcript.json --title "课程名"

输入结构（腾讯会议 wemeet-cloudrecording-webapi/v1/minutes/detail 响应）：
  {"code":0,"minutes":{"paragraphs":[{"pid":..,"start_time":毫秒,
    "end_time":毫秒,"sentences":[{"start_time":..,"end_time":..,
    "words":[{"text":..}]}]}]}}

输出结构（course_agent ingest 支持的 json 素材格式，start 单位为秒）：
  [{"start": 12.3, "text": "..."}, ...]
"""

from __future__ import annotations

import json
import sys


def convert(minutes_json: dict) -> list[dict]:
    data = minutes_json.get("minutes") or minutes_json
    paragraphs = data.get("paragraphs") or []
    if not paragraphs:
        raise SystemExit("输入 JSON 中没有 minutes.paragraphs，请确认文件来源")
    out = []
    for p in paragraphs:
        texts = []
        for s in p.get("sentences", []):
            for w in s.get("words", []):
                t = (w.get("text") or "").strip()
                if t:
                    texts.append(t)
        text = "".join(texts)
        if not text:
            continue
        start_ms = p.get("start_time") or 0
        out.append({"start": round(start_ms / 1000.0, 2), "text": text})
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 1
    with open(argv[1], encoding="utf-8") as f:
        data = json.load(f)
    rows = convert(data)
    with open(argv[2], "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    total_chars = sum(len(r["text"]) for r in rows)
    print(f"转换完成：{len(rows)} 段，共 {total_chars} 字 -> {argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
