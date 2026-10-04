"""双擎学习助手交互式 REPL：python main.py tutor。

七分支意图路由 + 交互式出题（出题后暂停等作答）+ 交卷判分出解析
+ SM-2 复习调度 + 知识图谱路径校验。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .agent import TutorAgent
from .config import load_config
from .llm import LLMError
from .sandbox import judge
from .utils import setup_logging


def _print_citations(cits: list[dict]) -> None:
    if not cits:
        return
    print("---")
    print("### 本次引用")
    for c in cits:
        t = f" · {c['time']}" if c.get("time") else ""
        print(f"| {c.get('type', '')} | {c.get('course', '')[:30]} | "
              f"{c.get('section', '')[:20]}{t} |")


def _grade_interactive(agent: TutorAgent, ex: dict, answer: str) -> None:
    """判一道题并输出双擎规范批改。"""
    kind = ex.get("kind", "")
    correct = None
    detail = ""
    seconds = 5.0
    if kind == "single_choice":
        u = answer.strip().upper()[:1]
        correct = u == str(ex.get("answer", "")).strip().upper()[:1]
        detail = f"正确答案 {ex.get('answer')}"
    elif kind == "true_false":
        u = answer.strip()
        r = str(ex.get("answer", "")).strip()
        correct = (u == r) or (u == "对" and r in ("正确", "是")) \
            or (u == "错" and r in ("错误", "否"))
        detail = f"正确答案 {r}"
    elif kind == "fill_blank":
        u = answer.strip()
        r = str(ex.get("answer", "")).strip()
        correct = u == r or r in u
        detail = f"参考答案 {r}"
    elif kind == "coding":
        lang = ex.get("language") or "python"
        jr = judge(answer, ex, lang)
        if not jr.get("runnable"):
            print(f"ℹ️ {jr.get('reason')}")
        else:
            correct = jr["passed"] == jr["total"]
            detail = f"在线测试通过 {jr['passed']}/{jr['total']}"
            for r in jr["results"]:
                mark = "✅" if r["passed"] else "❌"
                out = f" — {r['output'][:120]}" if r.get("output") else ""
                print(f"  {mark} 断言{r['idx'] + 1}{out}")
    else:  # short_answer 等：AI 评审
        try:
            d = agent._llm(json.dumps({
                "role_check": True,
                "question": ex.get("question", ""),
                "reference": ex.get("answer", ""),
                "user": answer}, ensure_ascii=False)
                if False else
                "你是阅卷老师。对照参考答案评审学生回答，只输出 JSON："
                '{"score": 0-100, "verdict": "掌握|部分掌握|未掌握", '
                '"feedback": "一句话点评"}\n\n'
                f"【题目】{ex.get('question', '')}\n"
                f"【参考答案】{ex.get('answer', '')}\n【学生回答】{answer}",
                "请只输出评分 JSON。", temperature=0.2, max_tokens=512)
            from .utils import extract_json
            dd = extract_json(d)
            correct = int(dd.get("score", 0)) >= 60
            detail = f"{dd.get('score')} 分 · {dd.get('verdict')} · {dd.get('feedback', '')}"
        except (LLMError, ValueError) as e:
            detail = f"AI 评审暂不可用（{e}），参考答案：{ex.get('answer', '')[:80]}"

    mark = "✅ 正确" if correct else ("⚠️ 部分正确" if correct is None else "❌ 错误")
    print(f"\n### 批改\n**你的答案：** {answer[:200]}\n**判定：** {mark}")
    if detail:
        print(f"**解析：** {detail}")
    kp = ex.get("kp") or ex.get("question", "")[:20]
    print(f"**关联知识点：** {kp}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="双擎学习助手（交互式）")
    ap.add_argument("--config", default=None)
    ap.add_argument("--course-root", default="output/特训营全部课程")
    args = ap.parse_args(argv or [])
    if args.course_root is None:
        args.course_root = "output/特训营全部课程"

    cfg = load_config(args.config)
    from .llm import MockLLM
    from .llm.factory import build_client_chains
    from .llm import Router
    local_chain, cloud_chain = build_client_chains(cfg)
    llm = Router(local_chain + cloud_chain)
    agent = TutorAgent(llm, course_root=args.course_root,
                       data_dir=cfg.data_dir)

    print("👋 我是双擎学习助手，可以帮你：\n")
    print("1. **学知识点** — 讲解课程内容，配代码示例，标注来源")
    print("2. **出题练习** — 含编程题、调试题、优化题，难度可调")
    print("3. **诊断薄弱点** — 找出你卡在哪")
    print("4. **梳理体系** — 零散知识点串成知识地图")
    print("5. **规划路径** — 分阶段学习计划")
    print("6. **跨课程串联** — 多门课知识点整合\n")
    print("直接说，例如：「讲讲乐观锁」「给我出事务的题，要有编程题」")
    print("（输入 exit 退出）\n")

    while True:
        try:
            msg = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            return 0
        if not msg:
            continue
        if msg.lower() in ("exit", "quit", "退出"):
            print("再见！")
            return 0

        t0 = time.time()
        try:
            out = agent.handle(msg)
        except LLMError as e:
            print(f"⚠️ 模型调用失败：{e}")
            continue
        print(f"\n{out.get('text', '')}")
        _print_citations(out.get("citations") or [])

        # 分支 B：交互式作答
        if out.get("branch") == "B" and out.get("exercises"):
            exs = out["exercises"]
            print("\n逐题作答（直接回复答案；编程题粘贴代码；回复 skip 跳过）\n")
            for i, ex in enumerate(exs):
                kind = ex.get("kind", "")
                tag = {"single_choice": "单选", "true_false": "判断",
                       "fill_blank": "填空", "short_answer": "简答",
                       "coding": "编程"}.get(kind, kind)
                print(f"\n【{i + 1}. {tag} · {ex.get('difficulty', '')}】"
                      f"{ex.get('question', '')}")
                if ex.get("options"):
                    for j, o in enumerate(ex.get("options", [])):
                        print(f"   {'ABCDEFGH'[j]}. {o}")
                if ex.get("starter_code"):
                    print(ex.get("starter_code", ""))
                try:
                    ans = input("你的答案> ").strip()
                except (EOFError, KeyboardInterrupt):
                    ans = "skip"
                if ans.lower() == "skip":
                    print("（跳过）")
                    continue
                try:
                    _grade_interactive(agent, ex, ans)
                except (LLMError, ValueError) as e:
                    print(f"⚠️ 评审失败：{e}")
                print()
            print("本章完成。需要「查漏补缺」或「下一章」随时说。")
        else:
            print(f"\n---\n耗时 {time.time() - t0:.0f}s\n")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
