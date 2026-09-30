"""课程智能改名：根据课程内容为产物目录生成易于理解的显示名称。

背景：批量课名来自文件夹（如"星星的个人会议室"、"001 人工智能产训特训"），
无法望文知义。本工具读取每门课的笔记开头，让本地大模型生成简洁明确的
课程名称（如"25、Python协程与并发编程入门"），写入 data/course_titles.json，
图形界面的课程列表优先使用覆盖名。

用法（后台运行，逐门处理）：
  python tools/rename_courses.py --output "output/特训营全部课程"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_agent.config import load_config  # noqa: E402
from course_agent.llm import OpenAICompatClient  # noqa: E402
from course_agent.utils import extract_json, truncate  # noqa: E402


def suggest_title(llm, source_text: str, current: str) -> str:
    prompt = (
        "你是课程命名助手。根据下面这份课程笔记的开头，给出一个简洁、便于理解的课程名称。"
        "要求：8~24 个汉字；体现核心主题与技术栈；不要日期、会议室名、序号以外的编号；"
        "只输出 JSON：{\"title\": \"课程名称\"}\n\n"
        f"【当前名称】{current}\n"
        f"【笔记开头】\n{truncate(source_text, 900)}\n"
    )
    raw = llm.chat(prompt, "请只输出 JSON。", temperature=0.2, max_tokens=1024)
    data = extract_json(raw)
    title = str(data.get("title", "")).strip()
    return truncate(title, 30) if title else ""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="课程智能改名")
    ap.add_argument("--output", default="output/特训营全部课程")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    cfg = load_config()
    llm = OpenAICompatClient(base_url=cfg.local.base_url, model=cfg.local.model,
                             api_key=cfg.local.api_key, timeout=cfg.local.timeout,
                             max_retries=cfg.local.max_retries)
    overrides_path = Path(cfg.data_dir) / "course_titles.json"
    overrides: dict = {}
    if overrides_path.is_file():
        overrides = json.loads(overrides_path.read_text(encoding="utf-8"))

    root = Path(args.output)
    dirs = sorted(d for d in root.iterdir()
                  if d.is_dir() and (d / "session.json").is_file())
    n = 0
    for d in dirs:
        cid = d.relative_to(root).as_posix()
        if cid in overrides:
            continue
        # 笔记来源优先级：review.md > notes.md
        src = ""
        for name in ("review.md", "notes.md"):
            f = d / name
            if f.is_file():
                src = f.read_text(encoding="utf-8")
                break
        if not src:
            continue
        cur_title = ""
        try:
            cur_title = json.loads((d / "session.json").read_text(encoding="utf-8")).get("title", "")
        except (json.JSONDecodeError, OSError):
            pass
        try:
            title = ""
            for attempt in range(3):  # 与批量任务共用本地大模型时可能排队超时，重试
                try:
                    title = suggest_title(llm, src, cur_title)
                    if title:
                        break
                except Exception as e:
                    last_err = str(e)
                    time.sleep(3 * (attempt + 1))
        except Exception as e:
            last_err = str(e)
        if title:
            overrides[cid] = title
            overrides_path.parent.mkdir(parents=True, exist_ok=True)
            overrides_path.write_text(json.dumps(overrides, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
            n += 1
            print(f"[{cid[:36]}] -> {title}", flush=True)
        else:
            print(f"[{cid[:36]}] 生成失败: {truncate(last_err, 90)}", flush=True)
        time.sleep(0.5)
        if args.limit and n >= args.limit:
            break
    print(f"\n完成：已为 {n} 门课程生成显示名 -> {overrides_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
