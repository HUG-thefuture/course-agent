"""录制记录清单解析：把"新人录制资料大礼包.md"转成 CourseAgent 批量清单。

输入格式（每条记录）：
  ## 标题
  - **日期**：2026-09-28 09:21:42
  - **录制文件**：[点击访问](https://meeting.tencent.com/crm/xxxx)
  - **密码**：`HCKB`     （或 `无`）

输出：
  courses.json: [{"seq": 1, "source": "...", "password": "...", "title": "..."}]
  其中 title 自动补序号与日期，避免"同名课"互相覆盖。

用法：
  python tools/parse_recording_catalog.py 新人录制资料大礼包.md courses.json
"""

from __future__ import annotations

import json
import re
import sys

RECORD_RE = re.compile(
    r"^##\s*(?P<title>.+?)\s*$"
    r".*?\*\*日期\*\*：(?P<date>[\d\- :]+)"
    r".*?\((?P<url>https://meeting\.tencent\.com/\S+?)\)"
    r".*?\*\*密码\*\*：`(?P<pwd>[^`]*)`",
    re.M | re.S)


def slug_date(date: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})", date.strip())
    if not m:
        return ""
    return f"{m.group(2)}{m.group(3)}_{m.group(4)}{m.group(5)}"


def parse(md_text: str) -> list[dict]:
    courses = []
    for m in RECORD_RE.finditer(md_text):
        title = m.group("title").strip()
        date = m.group("date")
        url = m.group("url").rstrip(").,，")
        pwd = m.group("pwd").strip()
        courses.append({
            "title": title,
            "date": date.strip(),
            "source": url,
            "password": "" if pwd in ("无", "-", "") else pwd,
        })
    return courses


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 1
    text = open(argv[1], encoding="utf-8").read()
    raw = parse(text)
    if not raw:
        print("未解析到任何记录，请检查文件格式")
        return 1
    seen: set[str] = set()
    out = []
    counter: dict[str, int] = {}
    for c in raw:
        if c["source"] in seen:
            continue  # 同一链接重复出现只保留一条
        seen.add(c["source"])
        counter[c["title"]] = counter.get(c["title"], 0) + 1
        seq = len(out) + 1
        full_title = f"{seq:03d} {c['title']} {slug_date(c['date'])}".strip()
        out.append({"seq": seq, "source": c["source"],
                    "password": c["password"], "title": full_title})
    with open(argv[2], "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    titles = {c["title"].split(" ", 1)[1].rsplit(" ", 1)[0] for c in out}
    print(f"解析完成：原始 {len(raw)} 条，去重后 {len(out)} 门课 -> {argv[2]}")
    print(f"涉及 {len(titles)} 个不同课程名；带密码 {sum(1 for c in out if c['password'])} 门")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
