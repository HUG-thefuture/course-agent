"""选择题答案自洽复核：用大模型逐题判定正确选项，修正标错答案的存量题目。

背景：模型偶发把单选题的 answer 字母标错（如 SQL 全称题标成 D 而正确是 A），
导致学生选对被判错。本工具对课程所有选择/多选题让大模型独立判定正确选项，
与存储 answer 不一致时以判定结果修正 sections.json。

用法（后台运行）：
  python tools/fix_choice_answers.py --output "output/特训营全部课程"
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

_LETTERS = "ABCDEFGH"


def check_choice(llm, ex: dict) -> str | None:
    """让模型判定正确选项字母；无法判定返回 None。"""
    opts = ex.get("options") or []
    if not opts:
        return None
    opt_lines = "\n".join(f"{_LETTERS[i]}. {o}" for i, o in enumerate(opts))
    prompt = (
        "你是阅卷老师。判断下面选择题的正确选项。\n"
        f"【题目】{ex.get('question', '')}\n【选项】\n{opt_lines}\n"
        "只输出 JSON：{\"answer\": \"正确选项字母\"}\n"
        "注意：如果多个选项都含正确表述，选表述最准确、最完整的一个；"
        "如果题目本身有问题（无正确选项），answer 填 \"?\"。"
    )
    raw = llm.chat(prompt, "请只输出 JSON。", temperature=0, max_tokens=64)
    data = extract_json(raw)
    ans = str(data.get("answer", "")).strip().upper()[:1]
    return ans if ans in _LETTERS else None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="选择题答案自洽复核")
    ap.add_argument("--output", default="output/特训营全部课程")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    cfg = load_config()
    llm = OpenAICompatClient(base_url=cfg.local.base_url, model=cfg.local.model,
                             api_key=cfg.local.api_key, timeout=cfg.local.timeout,
                             max_retries=cfg.local.max_retries)

    root = Path(args.output)
    dirs = sorted(d for d in root.iterdir()
                  if d.is_dir() and (d / "sections.json").is_file())
    if args.limit:
        dirs = dirs[: args.limit]

    fixed = checked = 0
    for di, d in enumerate(dirs, 1):
        secs_path = d / "sections.json"
        secs = json.loads(secs_path.read_text(encoding="utf-8"))
        changed = False
        for sec in secs:
            for e in sec.get("exercises", []):
                if e.get("type") not in ("single_choice", "multi_choice"):
                    continue
                checked += 1
                try:
                    truth = check_choice(llm, e)
                except Exception as e:
                    print(f"[{d.name[:34]}] 调用失败: {truncate(str(e), 70)}", flush=True)
                    time.sleep(2)
                    continue
                if truth and truth != "?" and truth != str(e.get("answer", "")).strip().upper():
                    old = str(e.get("answer", ""))
                    e["answer"] = truth
                    e["explanation"] = (f"（答案经复核修正：原 {old} → {truth}）"
                                        + str(e.get("explanation", "")))[:400]
                    changed += 1
                    print(f"  修正: {truncate(e.get('question', ''), 26)} {old} -> {truth}",
                          flush=True)
                time.sleep(0.3)
        if changed:
            secs_path.write_text(json.dumps(secs, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            print(f"[{di}/{len(dirs)}] {d.name[:38]} 修正 {changed} 处", flush=True)
    print(f"\n复核完成：共检查 {checked} 道选择题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
