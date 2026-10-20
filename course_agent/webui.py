"""CourseAgent 本地图形界面：标准库 HTTP 服务 + 浏览器单页应用。

启动：python -m course_agent.web   （或桌面快捷方式）
功能：
  - 课程列表（扫描 output 产物），查看笔记/关键节点/质检报告
  - 习题作答与自动判分：客观题本地判，简答/编程题调本地大模型对照参考答案评分
  - 新任务：提交素材路径/回放链接+密码，后台跑完整管线
仅监听 127.0.0.1，无外部依赖。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .config import AgentConfig, load_config
from .llm import LLMError
from .utils import setup_logging, slugify, truncate

_PORT = 8686
_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ 判分器
_TRUE_SET = {"对", "正确", "是", "true", "t", "√", "yes"}
_FALSE_SET = {"错", "错误", "否", "false", "f", "×", "no"}


def _norm_text(s: str) -> str:
    return re.sub(r"[\s，。,.、;；:：!！?？'\"`()（）\[\]【】]", "", (s or "").strip()).lower()


def grade_objective(exercise: dict, answer: str) -> tuple[bool, str]:
    """客观题确定性判分。返回 (是否正确, 说明)。"""
    t = exercise.get("type")
    ref = (exercise.get("answer") or "").strip()
    user = (answer or "").strip()
    if not user:
        return False, "未作答"
    if t == "single_choice":
        opts = [str(o) for o in exercise.get("options") or []]
        u = user.strip().upper()[:1]
        r = ref.strip().upper()
        if len(r) == 1 and r in "ABCDEFGH":
            return u == r, f"正确答案 {r}"
        # 兼容：模型把答案存成了选项文本而非字母——映射回字母再判。
        # 先找精确相等的选项；再做唯一包含匹配，避免"正确"/"不正确"子串互吞
        norm_ref = _norm_text(ref)
        exact = [i for i, o in enumerate(opts)
                 if _norm_text(o) and _norm_text(o) == norm_ref]
        if exact:
            return u == "ABCDEFGH"[exact[0]], f"正确答案 {'ABCDEFGH'[exact[0]]}"
        partial = [i for i, o in enumerate(opts)
                   if _norm_text(o) and norm_ref
                   and (norm_ref in _norm_text(o) or _norm_text(o) in norm_ref)]
        if len(partial) == 1:
            return u == "ABCDEFGH"[partial[0]], f"正确答案 {'ABCDEFGH'[partial[0]]}"
        return u == r[:1], f"正确答案 {ref}"
    if t == "multi_choice":
        u = set(re.findall(r"[A-H]", user.upper()))
        r = set(re.findall(r"[A-H]", ref.upper()))
        ok = u == r and bool(u)
        return ok, f"正确答案 {''.join(sorted(r))}"
    if t == "true_false":
        ul = user.lower()
        u = "对" if ul in _TRUE_SET else ("错" if ul in _FALSE_SET else user)
        r = "对" if ref.lower() in _TRUE_SET else ("错" if ref.lower() in _FALSE_SET else ref)
        ok = u == r
        return ok, f"正确答案 {r}"
    if t == "fill_blank":
        ok = _norm_text(user) == _norm_text(ref)
        if not ok and len(_norm_text(ref)) >= 4 and _norm_text(ref) in _norm_text(user):
            ok = True
        return ok, f"参考答案 {ref}"
    # 兜底：精确匹配
    return _norm_text(user) == _norm_text(ref), f"参考答案 {ref}"


def _subjective_prompt(exercise: dict, user_answer: str) -> str:
    if exercise.get("type") == "coding_practice":
        checks = "\n".join(f"- {p}" for p in exercise.get("check_points") or [])
        return (
            "你是编程题阅卷老师。对照参考实现与验收要点，评审学生代码。"
            "只输出 JSON：{\"score\": 0到100整数, \"verdict\": \"掌握|部分掌握|未掌握\", "
            "\"feedback\": \"不超过120字的修改建议\"}\n\n"
            f"【题目】{exercise.get('question', '')}\n"
            f"【验收要点】\n{checks or '（无）'}\n"
            f"【参考实现】\n{exercise.get('answer', '')}\n"
            f"【学生代码】\n{user_answer}\n"
        )
    return (
        "你是阅卷老师。对照参考答案评审学生回答：要点覆盖、有无错误。"
        "只输出 JSON：{\"score\": 0到100整数, \"verdict\": \"掌握|部分掌握|未掌握\", "
        "\"feedback\": \"不超过120字的点评\"}\n\n"
        f"【题目】{exercise.get('question', '')}\n"
        f"【参考答案】{exercise.get('answer', '')}\n"
        f"【学生回答】{user_answer}\n"
    )


def grade_subjective(llm, exercise: dict, user_answer: str) -> dict:
    """主观题（简答/编程）交给本地大模型评审。返回 {score, verdict, feedback}。"""
    from .llm.base import BaseLLM  # noqa: F401  (类型提示用)
    from .utils import extract_json

    raw = llm.chat(_subjective_prompt(exercise, user_answer),
                   "请只输出评分 JSON。", temperature=0.2, max_tokens=1024)
    data = extract_json(raw)
    score = int(data.get("score", 0))
    verdict = str(data.get("verdict", "未掌握"))
    feedback = str(data.get("feedback", ""))[:200]
    return {"score": max(0, min(100, score)), "verdict": verdict, "feedback": feedback}


# ------------------------------------------------------------------ 课程索引
def _course_dirs(cfg: AgentConfig) -> list[Path]:
    """扫描产物目录：output 下任何含 session.json 的目录。"""
    out = Path(cfg.output_dir)
    found: list[Path] = []
    if out.is_dir():
        for d in sorted(out.iterdir()):
            if (d / "session.json").is_file():
                found.append(d)
            elif d.is_dir():  # 批量子目录再进一层
                for sub in sorted(d.iterdir()):
                    if (sub / "session.json").is_file():
                        found.append(sub)
    return found


def _course_id(cfg: AgentConfig, d: Path) -> str:
    rel = d.relative_to(Path(cfg.output_dir)).as_posix()
    return rel


def _find_course(cfg: AgentConfig, cid: str) -> Optional[Path]:
    for d in _course_dirs(cfg):
        if _course_id(cfg, d) == cid:
            return d
    return None


# ------------------------------------------------------------------ 运行状态
_RUN_STATE: dict[str, Any] = {"running": False, "log": "", "result": None,
                              "batch": [], "done": 0, "total": 0}
_RUN_LOCK = threading.Lock()
_PROGRESS_LOCK = threading.Lock()  # 进度文件读改写互斥（多线程 HTTP + 交卷）


def parse_run_lines(text: str) -> list[dict]:
    """把多行输入解析为待处理项：每行 `链接 [提取码] [标题]` 或 `链接|提取码|标题`。

    腾讯会议回放链接会自动提取逐字稿；一行一项，# 开头为注释行，空行忽略。
    链接后紧跟的 3-8 位字母数字视作提取码，其剩余部分作标题。
    """
    items: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:  # 与 batch 清单同构
            parts = [x.strip() for x in line.split("|")]
            if len(parts) >= 3:
                items.append({"source": parts[0], "password": parts[1], "title": parts[2]})
            elif len(parts) == 2:
                key = "password" if parts[0].startswith("http") else "title"
                items.append({"source": parts[0], "password": parts[1] if key == "password" else "",
                              "title": parts[1] if key == "title" else ""})
            elif parts and parts[0]:
                items.append({"source": parts[0], "password": "", "title": ""})
            continue
        m = re.search(r"https?://\S+|[^\s|]+\.(?:mp4|mkv|mov|avi|webm|mp3|m4a|srt|vtt|json|txt|md|pdf)\b",
                      line, re.I)
        if not m:  # 纯标题路径无法识别，整行当源（交给下游报错，保持隔离）
            items.append({"source": line, "password": "", "title": ""})
            continue
        source = m.group(0)
        rest = (line[:m.start()] + " " + line[m.end():]).strip()
        password, title = "", rest
        toks = rest.split(None, 1)
        if toks and re.fullmatch(r"[A-Za-z0-9]{3,8}", toks[0]):
            password, title = toks[0], (toks[1] if len(toks) > 1 else "")
        items.append({"source": source, "password": password, "title": title})
    return [i for i in items if i["source"]]


def _run_one(input_src: str, password: str, title: str, cfg: AgentConfig,
             mem=None) -> dict:
    """处理单个课程素材：加载 -> 整理 -> 落盘，返回结果 dict。"""
    from .ingest.loader import load_source
    from .outputs import write_all
    from .pipeline.orchestrator import Orchestrator

    material = load_source(input_src, password=password, workdir=cfg.data_dir)
    course_title = title or material.title
    llm = _build_llm(cfg)
    orch = Orchestrator(llm, cfg, memory=mem)
    result = orch.run(material, title=course_title)
    outdir = Path(cfg.output_dir) / "webui" / result.session_id
    result.output_dir = str(outdir)
    write_all(result, outdir)
    return {"ok": result.passed, "title": course_title,
            "score": result.qa.score if result.qa else 0, "dir": str(outdir)}


def _run_batch_async(items: list[dict], cfg: AgentConfig) -> None:
    """后台串行处理多个课程素材，逐项更新 _RUN_STATE（含 batch 明细与进度）。"""
    import logging
    from .memory import Memory

    log = logging.getLogger("webui.run")

    def run():
        mem = None
        try:
            mem = Memory(Path(cfg.data_dir) / "memory.db")
            with _RUN_LOCK:
                _RUN_STATE["batch"] = [
                    {"title": it.get("title") or it["source"][-40:], "status": "pending",
                     "score": 0, "error": ""} for it in items]
                _RUN_STATE["done"] = 0
                _RUN_STATE["total"] = len(items)
                _RUN_STATE["result"] = None
            ok_n = 0
            new_dirs: list[Path] = []
            for idx, it in enumerate(items):
                label = it.get("title") or it["source"][-40:]
                with _RUN_LOCK:
                    _RUN_STATE["log"] = f"[{idx + 1}/{len(items)}] 处理中：{label}"
                    _RUN_STATE["batch"][idx]["status"] = "running"
                try:
                    r = _run_one(it["source"], it.get("password", ""),
                                 it.get("title", ""), cfg, mem=mem)
                    with _RUN_LOCK:
                        b = _RUN_STATE["batch"][idx]
                        b.update(status="done" if r["ok"] else "qa_failed",
                                 score=r["score"])
                    if r["ok"]:
                        ok_n += 1
                        new_dirs.append(Path(r["dir"]))
                except Exception as e:  # 单项失败不拖垮整批
                    log.error("运行失败 %s: %s", it["source"], e)
                    with _RUN_LOCK:
                        _RUN_STATE["batch"][idx]["status"] = "failed"
                        _RUN_STATE["batch"][idx]["error"] = truncate(str(e), 200)
                with _RUN_LOCK:
                    _RUN_STATE["done"] = idx + 1
            with _RUN_LOCK:
                _RUN_STATE["result"] = {"ok": True, "batch": True,
                                        "total": len(items), "passed": ok_n}
            with _RUN_LOCK:
                _RUN_STATE["log"] = f"批量完成：{ok_n}/{len(items)} 门通过质检"
            # 新课程自动归入课程组（增量聚类+整合复习更新）；失败只记日志不拖垮批次
            try:
                _autogroup_new(new_dirs, cfg)
            except Exception as e:  # 归组失败不影响课程产物
                log.error("自动归组失败: %s", e)
                with _RUN_LOCK:
                    _RUN_STATE["log"] += f"；课程组更新失败：{truncate(str(e), 120)}"
        except Exception as e:  # 整批无法启动（如 memory 打不开）
            log.error("批量运行异常: %s", e)
            with _RUN_LOCK:
                _RUN_STATE["result"] = {"ok": False, "error": truncate(str(e), 300)}
                _RUN_STATE["log"] = f"失败：{truncate(str(e), 160)}"
        finally:
            if mem is not None:
                mem.close()
            with _RUN_LOCK:
                _RUN_STATE["running"] = False

    threading.Thread(target=run, daemon=True).start()


def _build_llm(cfg: AgentConfig):
    from .llm import MockLLM, Router
    from .llm.factory import build_client_chains
    local_chain, cloud_chain = build_client_chains(cfg)
    return Router(local_chain + cloud_chain)


# ---------------------------------------------------------- 新课程自动归组
_GROUP_LOCK = threading.Lock()  # 补归组与批量归组互斥（防同组 组.json 竞态写）


def _autogroup_backfill(cfg: AgentConfig) -> None:
    """启动时补归组：已过质检但未进任何课程组的历史课程（如服务停机期间
    跑完的课）。只做规则/嵌入归组（不阻塞、不跑 LLM 整合，整合留待下次
    新课程触发或手动 merge_courses.py）。"""
    import logging
    from . import grouping

    log = logging.getLogger("webui.group")
    try:
        with _GROUP_LOCK:
            missing = grouping.missing_passed_courses(cfg)
            if missing:
                log.info("启动补归组 %d 门课程", len(missing))
                grouping.place_new_courses(missing, cfg, llm=None)
    except Exception as e:  # 补归组失败不影响服务启动
        log.error("启动补归组失败: %s", e)


def _autogroup_new(dirs: list[Path], cfg: AgentConfig) -> None:
    """质检通过的新课程自动归入课程组（增量内容聚类，嵌入服务不可用时
    自动退化为标题规则）；受影响组的整合复习在后台线程用 LLM 刷新。"""
    from . import grouping

    out_root = Path(cfg.output_dir)
    items: list[tuple[str, Path]] = []
    for d in dirs:
        d = Path(d)
        try:
            cid = d.relative_to(out_root).as_posix()
        except ValueError:
            cid = d.name
        items.append((cid, d))
    if not items:
        return
    with _GROUP_LOCK:
        info = grouping.place_new_courses(items, cfg, llm=None)  # 只嵌入新课程块，秒级
    if info["groups"]:
        threading.Thread(target=_regen_groups,
                         args=(info["groups"], cfg), daemon=True).start()


def _regen_groups(names: list[str], cfg: AgentConfig) -> None:
    """后台重新生成受影响组的整合复习（LLM 重活）；失败保留既有/占位文档。"""
    import logging
    from . import grouping

    log = logging.getLogger("webui.group")
    try:
        llm = Handler.llm or _build_llm(cfg)
    except Exception as e:
        log.error("整合复习刷新跳过（LLM 不可用）: %s", e)
        return
    out_root = Path(cfg.output_dir)
    groups = grouping.load_groups(out_root)
    courses = dict(grouping.passed_courses(out_root))
    for name in names:
        g = groups.get(name)
        if not g:
            continue
        dirs = [courses[cid] for cid in g["ids"] if cid in courses]
        if not dirs:
            continue
        try:
            text = grouping.gen_integration(llm, name, dirs)
            grouping.write_atomic(g["dir"] / "整合复习.md", text)
            log.info("整合复习已刷新：%s（%d 门课）", name, len(dirs))
        except Exception as e:
            log.error("整合复习刷新失败 %s: %s", name, e)


# ------------------------------------------------------------------ 在线测试
def _code_names(code: str) -> set[str]:
    """静态分析用户代码：返回顶层函数名集合（断言在模块层调用，只看得到这些）。"""
    import ast
    defined: set[str] = set()
    try:
        tree = ast.parse(code)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
    except (SyntaxError, ValueError):
        pass
    return defined


def _test_called_names(tests: list[str]) -> set[str]:
    called: set[str] = set()
    for t in tests:
        for m in re.finditer(r"([A-Za-z_]\w*)\s*\(", t):
            name = m.group(1)
            if name not in ("assert", "print", "isinstance", "len", "range",
                            "str", "int", "float", "list", "dict", "set",
                            "abs", "round", "sorted", "Exception"):
                called.add(name)
    return called


def run_code_tests(code: str, tests: list[str], language: str,
                   timeout: int = 10) -> dict:
    """编程题在线测试：把用户代码与每条断言拼接后真实执行。

    支持 python（本机解释器）与 javascript（需本机 node）；
    每条断言独立运行，返回逐条通过/失败与输出。
    超时杀整棵进程树；提前 exit() 会被前置防护拦下（否则断言被跳过误判通过）。
    """
    import shutil

    from .sandbox import _JS_GUARD, _PY_GUARD

    if language == "python":
        runner = [sys.executable]
    elif language == "javascript" and shutil.which("node"):
        runner = [shutil.which("node")]
    else:
        return {"runnable": False,
                "reason": "该题不支持本机自动测试（框架/页面类题目请交卷后由 AI 评审）"}

    results = []
    for i, test in enumerate(tests):
        script = code.rstrip() + "\n\n" + test + "\n"
        if language == "python":
            script = _PY_GUARD + script
        else:
            # node 无内置 assert，注入与浏览器约定一致的断言垫片
            script = ("function assert(cond, msg) { if (!cond) "
                      "throw new Error('AssertionError' + (msg ? ': ' + msg : '')); }\n"
                      + _JS_GUARD + script)
        results.append(_exec_test(runner, script, language, timeout, i, test))
    passed = sum(1 for r in results if r["passed"])
    return {"runnable": True, "results": results,
            "passed": passed, "total": len(results)}


def _exec_test(runner: list, script: str, language: str, timeout: int,
               i: int, test: str) -> dict:
    """把单条断言脚本写入临时文件执行并返回判题结果。"""
    import tempfile

    from .sandbox import run_process

    fd, tmp = tempfile.mkstemp(suffix=".py" if language == "python" else ".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        rc, out, err, timed_out = run_process([*runner, tmp], timeout=timeout)
        if timed_out:
            return {"idx": i, "test": test, "passed": False,
                    "output": f"超时（>{timeout}s），检查是否存在死循环"}
        ok = rc == 0
        out = (err or out or "").strip()
        return {"idx": i, "test": test, "passed": ok,
                "output": truncate(out, 400)}
    finally:
        Path(tmp).unlink(missing_ok=True)


def _backfill_tests(llm, exercise: dict, starter_code: str = "",
                    user_code: str = "") -> dict:
    """为没有 tests 的存量编程题现场生成在线测试断言。

    user_code 非空时（重生成模式），断言必须调用学生代码里实际定义的函数名。
    """
    from .utils import extract_json

    anchor = ""
    if user_code.strip():
        anchor = f"\n【学生当前代码（tests 必须调用其中实际定义的函数名）】\n{truncate(user_code, 800)}\n"
    elif starter_code.strip():
        anchor = f"\n【代码骨架（tests 须与其中的函数名一致）】\n{truncate(starter_code, 600)}\n"
    prompt = (
        "根据编程题与其参考实现，生成在线测试断言。只输出 JSON："
        '{"language": "python|javascript|other", '
        '"tests": ["每条一行可独立执行的断言，python 用 assert，javascript 用 assert(条件)", ...]}'
        "要求：2~4 条，覆盖正常与边界情况；断言基于题干规定的函数名与参考实现语义；"
        "若题目是框架/页面类无法断言验证，language 为 other 且 tests 为空数组。\n"
        f"{anchor}\n"
        f"【题目】{exercise.get('question', '')}\n"
        f"【参考实现】\n{exercise.get('answer', '')}\n"
        f"【验收要点】{json.dumps(exercise.get('check_points') or [], ensure_ascii=False)}\n"
    )
    raw = llm.chat(prompt, "请只输出 JSON。", temperature=0.2, max_tokens=1024)
    data = extract_json(raw)
    lang = data.get("language")
    tests = [str(t).strip() for t in data.get("tests") or [] if str(t).strip()]
    return {"language": lang if lang in ("python", "javascript", "other") else "other",
            "tests": tests[:4]}


def _gen_quiz_review(llm, course_title: str, section_title: str,
                     items: list[dict], review_note: str) -> dict:
    """交卷后自动生成：逐题解析 + 薄弱点强化知识 + 复习建议。"""
    from .utils import extract_json, truncate as _t

    rows = []
    for it in items:
        rows.append({
            "idx": it["idx"], "type": it.get("type", ""),
            "question": _t(it.get("question", ""), 120),
            "user_answer": _t(str(it.get("user_answer", "")), 80),
            "correct": it.get("correct"),
            "reference": _t(str(it.get("reference", "")), 120),
        })
    prompt = (
        "你是教研老师。学生刚完成一章测验，判分结果已给出。请生成针对性的解析与强化复习内容。\n"
        "要求：\n"
        "- items 覆盖每一题：答错的题深度解析（正确答案依据、学生错在哪、正确思路）；答对的题给巩固要点；\n"
        "- weak_points 只提炼答错题目涉及的知识点（全对则给进阶强化），每条给出强化讲解，150~250 字，"
        "结合【课程复习笔记】的真实内容，不要编造；\n"
        "- suggestions 给 2~3 条后续复习建议；\n"
        "- 只输出 JSON：{\"overall\": \"整体评价60~120字\", "
        "\"items\": [{\"idx\": 0, \"analysis\": \"该题解析80~200字\"}], "
        "\"weak_points\": [{\"topic\": \"\", \"content\": \"\"}], "
        "\"suggestions\": [\"\", \"\"]}\n\n"
        f"【课程】{course_title} · {section_title}\n"
        f"【判分结果】\n{json.dumps(rows, ensure_ascii=False)}\n"
        f"【课程复习笔记】\n{_t(review_note, 2600)}\n"
    )
    raw = llm.chat(prompt, "请只输出 JSON。", temperature=0.3, max_tokens=2048)
    data = extract_json(raw)
    items_out = []
    for it in data.get("items") or []:
        if isinstance(it, dict) and "analysis" in it:
            items_out.append({"idx": int(it.get("idx", 0)),
                              "analysis": str(it["analysis"])[:400]})
    weak = []
    for w in data.get("weak_points") or []:
        if isinstance(w, dict) and w.get("topic"):
            weak.append({"topic": str(w["topic"])[:60],
                         "content": str(w.get("content", ""))[:400]})
    sugg = [str(s)[:120] for s in data.get("suggestions") or [] if str(s).strip()][:3]
    return {"overall": str(data.get("overall", ""))[:300],
            "items": items_out, "weak_points": weak, "suggestions": sugg}


def _append_quiz_report_md(course_dir: Path, section_title: str, review: dict) -> Path:
    """把本次测验解析与强化知识追加到课程的 强化复习.md。"""
    md_path = course_dir / "强化复习.md"
    stamp = time.strftime("%Y-%m-%d %H:%M")
    lines = [f"\n## {section_title} · 测验解析（{stamp}）", ""]
    if review.get("overall"):
        lines += [f"**整体评价**：{review['overall']}", ""]
    if review.get("items"):
        lines += ["### 逐题解析", ""]
        for it in sorted(review["items"], key=lambda x: x["idx"]):
            lines += [f"- **第 {it['idx'] + 1} 题**：{it['analysis']}", ""]
    if review.get("weak_points"):
        lines += ["### 📌 薄弱点强化知识", ""]
        for w in review["weak_points"]:
            lines += [f"- **{w['topic']}**：{w['content']}", ""]
    if review.get("suggestions"):
        lines += ["### 🧭 复习建议", ""]
        for s in review["suggestions"]:
            lines += [f"- {s}", ""]
    with md_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return md_path


def _course_titles_override(cfg: AgentConfig) -> dict:
    """课程显示名覆盖表（由 tools/rename_courses.py 生成）。"""
    p = Path(cfg.data_dir) / "course_titles.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


# ------------------------------------------------------------------ 进度存取
def _progress_path(cfg: AgentConfig, cid: str) -> Path:
    return Path(cfg.data_dir) / "webui_progress" / (re.sub(r"[^\w.-]", "_", cid) + ".json")


def _load_progress(cfg: AgentConfig, cid: str) -> dict:
    p = _progress_path(cfg, cid)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_progress(cfg: AgentConfig, cid: str, data: dict) -> None:
    p = _progress_path(cfg, cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")  # 原子替换：写一半被读不会得到空进度
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    cfg: AgentConfig = None  # type: ignore[assignment]
    llm = None
    server_version = "CourseAgentUI/1.0"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    # ------------------------------------------------------------- helpers
    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str, code: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ------------------------------------------------------------- routing
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._html(INDEX_HTML)
            if u.path == "/api/courses":
                overrides = _course_titles_override(self.cfg)
                show_all = (q.get("all") or [""])[0] in ("1", "true", "yes")
                rows = []
                for d in _course_dirs(self.cfg):
                    try:
                        s = json.loads((d / "session.json").read_text(encoding="utf-8"))
                        passed = bool(s.get("passed", False))
                        if not passed and not show_all:
                            continue  # 生成失败（未过质检）不入课程列表
                        cid = _course_id(self.cfg, d)
                        rows.append({"id": cid,
                                     "title": overrides.get(cid) or s.get("title", d.name),
                                     "passed": passed,
                                     "score": s.get("qa_score", 0),
                                     "sections": s.get("sections", 0),
                                     "exercises": s.get("exercises", 0),
                                     "date": d.name[:14]})
                    except (json.JSONDecodeError, OSError):
                        continue
                rows.sort(key=lambda r: r["id"], reverse=True)
                return self._json({"courses": rows})
            if u.path == "/api/course":
                d = _find_course(self.cfg, (q.get("id") or [""])[0])
                if not d:
                    return self._json({"error": "not found"}, 404)
                sections = json.loads((d / "sections.json").read_text(encoding="utf-8"))
                session = json.loads((d / "session.json").read_text(encoding="utf-8"))
                report = (d / "review_report.md").read_text(encoding="utf-8") \
                    if (d / "review_report.md").is_file() else ""
                notes = (d / "notes.md").read_text(encoding="utf-8") \
                    if (d / "notes.md").is_file() else ""
                review = (d / "review.md").read_text(encoding="utf-8") \
                    if (d / "review.md").is_file() else ""
                return self._json({"id": _course_id(self.cfg, d), "session": session,
                                   "sections": sections, "notes": notes,
                                   "review": review, "report": report,
                                   "progress": _load_progress(self.cfg, _course_id(self.cfg, d))})
            if u.path == "/api/progress":
                cid = (q.get("course") or [""])[0]
                return self._json(_load_progress(self.cfg, cid))
            if u.path == "/api/run/status":
                with _RUN_LOCK:
                    return self._json(dict(_RUN_STATE))
            if u.path == "/api/groups":
                return self._json({"groups": self._group_rows()})
            if u.path == "/api/group":
                return self._json(self._group_detail((q.get("name") or [""])[0]))
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # 防单请求异常拖垮服务
            return self._json({"error": str(e)}, 500)

    # ---------------------------------------------------- 课程组（内容整合）
    def _group_dirs(self) -> list[Path]:
        gdir = Path(self.cfg.output_dir) / "课程整合"
        if not gdir.is_dir():
            return []
        return [d for d in sorted(gdir.iterdir()) if (d / "整合复习.md").is_file()]

    def _group_name(self, d: Path) -> str:
        gp = d / "组.json"
        if gp.is_file():
            try:
                return json.loads(gp.read_text(encoding="utf-8")).get("name") or d.name
            except (json.JSONDecodeError, OSError):
                pass
        return d.name

    def _group_courses(self, d: Path) -> list[tuple[str, str]]:
        """组内成员：返回 [(course_id, 展示标题)]，标题走改名覆盖表。"""
        gp = d / "组.json"
        ids: list[str] = []
        if gp.is_file():
            try:
                ids = json.loads(gp.read_text(encoding="utf-8")).get("courses", [])
            except (json.JSONDecodeError, OSError):
                ids = []
        overrides = _course_titles_override(self.cfg)
        rows: list[tuple[str, str]] = []
        for cid in ids:
            cd = _find_course(self.cfg, cid)
            if cd is None:
                continue
            try:
                s = json.loads((cd / "session.json").read_text(encoding="utf-8"))
                if not s.get("passed", False):
                    continue  # 未过质检的课不进课程组
                rows.append((cid, overrides.get(cid) or s.get("title", cid)))
            except (json.JSONDecodeError, OSError):
                continue  # session 损坏的课无法确认状态，不展示
        return rows

    def _group_rows(self) -> list[dict]:
        out = []
        for d in self._group_dirs():
            courses = self._group_courses(d)
            out.append({"name": self._group_name(d),
                        "count": len(courses),
                        "size": (d / "整合复习.md").stat().st_size})
        out.sort(key=lambda r: -r["count"])
        return out

    def _group_detail(self, gname: str) -> dict:
        for d in self._group_dirs():
            if self._group_name(d) == gname or d.name == gname:
                return {"name": self._group_name(d),
                        "courses": [{"id": cid, "title": t}
                                    for cid, t in self._group_courses(d)],
                        "doc": (d / "整合复习.md").read_text(encoding="utf-8")}
        return {"error": "课程组不存在"}

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        body = self._body()
        try:
            if u.path == "/api/grade":
                return self._json(self._handle_grade(body))
            if u.path == "/api/run_tests":
                cid = str(body.get("course") or "")
                try:
                    si = int(body.get("section", 0))
                    idx = int(body.get("idx", 0))
                except (TypeError, ValueError):
                    return self._json({"error": "section/idx 需为整数"}, 400)
                code = str(body.get("code") or "")
                d = _find_course(self.cfg, cid)
                ex: dict = {}
                if d and (d / "sections.json").is_file():
                    try:
                        secs = json.loads((d / "sections.json").read_text(encoding="utf-8"))
                        if 0 <= si < len(secs):
                            exs = secs[si].get("exercises") or []
                            if 0 <= idx < len(exs):
                                ex = exs[idx]
                    except (json.JSONDecodeError, OSError):
                        pass
                tests = [str(t) for t in ex.get("tests") or []]
                lang = str(ex.get("language") or "")
                regen = bool(body.get("regen"))
                if regen or not tests or not lang:
                    # 缺断言或用户要求按自己的代码重生成：现场生成并回写缓存
                    try:
                        starter = str(ex.get("starter_code") or "")
                        back = _backfill_tests(self.llm, ex or {
                            "question": str(body.get("question") or ""),
                            "answer": str(body.get("answer") or "")},
                            starter_code=starter, user_code=code)
                        tests, lang = back["tests"], back["language"]
                        if ex and d and (d / "sections.json").is_file() and tests:
                            secs = json.loads((d / "sections.json").read_text(encoding="utf-8"))
                            exs = secs[si].get("exercises") or []
                            if 0 <= idx < len(exs):
                                exs[idx]["tests"] = tests
                                exs[idx]["language"] = lang
                                (d / "sections.json").write_text(
                                    json.dumps(secs, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
                    except (LLMError, ValueError) as e:
                        return self._json({"runnable": False,
                                           "reason": f"生成测试失败：{truncate(str(e), 120)}，"
                                                     "可交卷后由 AI 评审"})
                if not code.strip():
                    return self._json({"error": "请先在代码区编写实现"}, 400)
                if not tests:
                    return self._json({"runnable": False,
                                       "reason": "该题无法自动断言，请交卷后由 AI 评审"})
                result = run_code_tests(code, tests, lang)
                # 函数名不匹配诊断：测试调用的名字 vs 用户代码定义的名字
                failed_nameerrors = [r for r in result.get("results", [])
                                     if "NameError" in (r.get("output") or "")]
                if failed_nameerrors:
                    expected = sorted(_test_called_names(tests))
                    defined = sorted(_code_names(code))
                    result["mismatch"] = {
                        "expected": expected, "defined": defined,
                        "hint": (f"测试断言调用的函数：{', '.join(expected) or '（无）'}；"
                                 f"你的代码定义的函数：{', '.join(defined) or '（无）'}。"
                                 "两者不一致——请统一命名，或点击下方按钮按你的代码重新生成测试。")}
                return self._json(result)
            if u.path == "/api/progress":
                cid = body.get("course", "")
                if not cid:
                    return self._json({"error": "course required"}, 400)
                inc = body.get("data") or {}
                with _PROGRESS_LOCK:
                    data = _load_progress(self.cfg, cid)
                    for k, v in inc.items():
                        if k == "sections" and isinstance(v, dict) \
                                and isinstance(data.get("sections"), dict):
                            data["sections"].update(v)  # 深合并：只更新提交的章
                        else:
                            data[k] = v
                    _save_progress(self.cfg, cid, data)
                return self._json({"ok": True})
            if u.path == "/api/run":
                src = (body.get("input") or "").strip()
                if not src:
                    return self._json({"error": "input required"}, 400)
                # 统一按行解析（单行也支持 `链接 提取码 标题`）；空字段用表单栏兜底
                items = parse_run_lines(src)
                pw, ti = body.get("password", ""), body.get("title", "")
                if len(items) == 1:
                    items[0]["password"] = items[0]["password"] or pw
                    items[0]["title"] = items[0]["title"] or ti
                if not items:
                    return self._json({"error": "没有可识别的课程链接/路径"}, 400)
                with _RUN_LOCK:  # 检查与置位同一临界区，防双击双启动
                    if _RUN_STATE["running"]:
                        return self._json({"error": "已有任务在运行，请稍候"}, 409)
                    _RUN_STATE.update(running=True, log="排队中…", result=None,
                                      batch=[], done=0, total=len(items))
                _run_batch_async(items, self.cfg)
                return self._json({"ok": True, "count": len(items)})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    # ------------------------------------------------------------- grading
    def _handle_grade(self, body: dict) -> dict:
        cid = body.get("course", "")
        d = _find_course(self.cfg, cid)
        if not d:
            return {"error": "course not found"}
        sections = json.loads((d / "sections.json").read_text(encoding="utf-8"))
        si = int(body.get("section", 0))
        if not (0 <= si < len(sections)):
            return {"error": "section out of range"}
        exercises = sections[si].get("exercises", [])
        answers = body.get("answers") or []
        results = []
        llm_err = None
        for i, ex in enumerate(exercises):
            user = str(answers[i] if i < len(answers) else "")
            t = ex.get("type")
            if t in ("single_choice", "multi_choice", "true_false", "fill_blank"):
                ok, note = grade_objective(ex, user)
                results.append({"idx": i, "correct": ok,
                                "score": 100 if ok else 0,
                                "note": note, "reference": ex.get("answer", "")})
            else:  # short_answer / coding_practice -> LLM 评审
                if not user.strip():
                    results.append({"idx": i, "correct": False, "score": 0,
                                    "note": "未作答", "reference": ex.get("answer", "")})
                    continue
                try:
                    r = grade_subjective(self.llm, ex, user)
                    results.append({"idx": i, "correct": r["score"] >= 60,
                                    "score": r["score"], "verdict": r["verdict"],
                                    "feedback": r["feedback"],
                                    "reference": ex.get("answer", "")})
                except (LLMError, ValueError) as e:
                    llm_err = truncate(str(e), 160)
                    results.append({"idx": i, "correct": None, "score": None,
                                    "note": f"评分服务不可用：{llm_err}",
                                    "reference": ex.get("answer", "")})
        # 记入进度（与 /api/progress、其他交卷请求互斥）
        with _PROGRESS_LOCK:
            prog = _load_progress(self.cfg, cid)
            sec_prog = prog.setdefault("sections", {}).setdefault(str(si), {})
            for r in results:
                sec_prog[str(r["idx"])] = {"user": answers[r["idx"]] if r["idx"] < len(answers) else "",
                                           "score": r["score"], "correct": r["correct"]}
            _save_progress(self.cfg, cid, prog)
        graded = [r for r in results if r["score"] is not None]

        # 交卷后自动生成：逐题解析 + 薄弱点强化知识 + 复习建议（追加到 强化复习.md）
        review = None
        try:
            review_note = ""
            for name in ("review.md", "notes.md"):
                f = d / name
                if f.is_file():
                    review_note = f.read_text(encoding="utf-8")
                    break
            course_title = cid
            try:
                course_title = json.loads((d / "session.json").read_text(
                    encoding="utf-8")).get("title", cid)
            except (json.JSONDecodeError, OSError):
                pass
            items_for_review = []
            for i, ex in enumerate(exercises):
                ua = str(answers[i]) if i < len(answers) else ""
                r = next((r for r in results if r["idx"] == i), None)
                items_for_review.append({"idx": i, "type": ex.get("type", ""),
                                         "question": ex.get("question", ""),
                                         "user_answer": ua,
                                         "correct": r["correct"] if r else None,
                                         "reference": ex.get("answer", "")})
            review = _gen_quiz_review(self.llm, course_title,
                                      sections[si].get("title", ""),
                                      items_for_review, review_note)
            review["saved_to"] = _append_quiz_report_md(
                d, sections[si].get("title", ""), review).name
        except (LLMError, ValueError) as e:
            review = {"error": truncate(str(e), 160)}

        return {"results": results,
                "summary": {"graded": len(graded),
                            "correct": sum(1 for r in graded if r["correct"]),
                            "avg": round(sum(r["score"] for r in graded) / len(graded)) if graded else 0,
                            "llm_error": llm_err},
                "review": review}


# ------------------------------------------------------------------ 页面
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CourseAgent 课程助手</title>
<style>
:root{--bg:#0f1420;--card:#1a2130;--line:#2a3347;--fg:#e8ecf4;--dim:#8b96ab;--acc:#5b9dff;--ok:#4ade80;--bad:#f87171;--mid:#eab308}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.7 "Microsoft YaHei",system-ui,sans-serif}
a{color:var(--acc);text-decoration:none}header{padding:14px 22px;border-bottom:1px solid var(--line);display:flex;gap:18px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0}
nav button{background:none;border:1px solid var(--line);color:var(--dim);padding:6px 14px;border-radius:8px;cursor:pointer}
nav button.on{color:var(--fg);border-color:var(--acc)}
main{max-width:1080px;margin:0 auto;padding:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px;margin-bottom:14px}
.dim{color:var(--dim)}.ok{color:var(--ok)}.bad{color:var(--bad)}
table{width:100%;border-collapse:collapse}td,th{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left}
button.pri{background:var(--acc);border:none;color:#08101f;padding:8px 18px;border-radius:8px;cursor:pointer;font-weight:600}
button.mini{background:none;border:1px solid var(--line);color:var(--dim);padding:4px 10px;border-radius:6px;cursor:pointer}
input,textarea{background:#111827;border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;width:100%;font:inherit}
input[type=radio],input[type=checkbox]{width:auto;padding:0;accent-color:var(--acc);transform:scale(1.15);vertical-align:middle;margin-right:8px}
textarea{min-height:90px;resize:vertical}
textarea.code{font-family:Consolas,"Courier New",monospace;font-size:13.5px;line-height:1.6;min-height:170px;tab-size:4;white-space:pre;resize:vertical;border:none;border-radius:0}
.editor{display:flex;background:#0d1117;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:8px 0}
.editor .gutter{margin:0;padding:10px 8px 10px 12px;color:#5a6a85;background:#0d1117;text-align:right;user-select:none;font-family:Consolas,"Courier New",monospace;font-size:13.5px;line-height:1.6;min-width:36px;white-space:pre;overflow:hidden}
.editor textarea.code{flex:1;min-height:170px}
.ex{border-left:3px solid var(--acc);padding:10px 14px;margin:12px 0;background:#151c2b;border-radius:0 8px 8px 0}
.tag{font-size:12px;color:var(--dim);border:1px solid var(--line);padding:1px 8px;border-radius:99px;margin-left:6px}
.res{margin-top:6px;padding:8px 12px;border-radius:8px}
.res.ok{background:#12311f}.res.bad{background:#371d1d}.res.mid{background:#332b12}
pre{white-space:pre-wrap;word-break:break-word;background:#111827;padding:14px;border-radius:10px;overflow-x:auto;font-size:13.5px;line-height:1.75}
.kp{display:block;margin:4px 0}.kp b{color:var(--acc)}
.row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}.row>div{flex:1;min-width:220px}
.hide{display:none}
</style></head><body>
<header><h1>📚 CourseAgent 课程助手</h1><nav>
<button id="nav-courses" class="on" onclick="show('courses')">我的课程</button>
<button id="nav-groups" onclick="show('groups')">课程组</button>
<button id="nav-run" onclick="show('run')">新任务</button>
</nav></header>
<main>
<section id="view-courses"><div class="card" id="course-list"><span class="dim">加载中…</span></div></section>

<section id="view-course" class="hide"><div id="course-body"></div></section>

<section id="view-groups" class="hide"><div class="card" id="group-list"><span class="dim">加载中…</span></div></section>

<section id="view-groupdetail" class="hide"><div id="group-body"></div></section>

<section id="view-run" class="hide"><div class="card"><h3>➕ 新课程整理任务</h3>
<label>素材（一行一个：腾讯回放链接 / 文件路径）。腾讯会议回放自动提取 AI 逐字稿</label>
<textarea id="r-in" rows="5" placeholder="https://meeting.tencent.com/crm/xxxx&#10;https://meeting.tencent.com/crm/yyyy 提取码 课程标题&#10;D:/录屏/xx.mp4 课程标题"></textarea>
<p class="dim">单行时提取码/标题可用下方独立栏；多行时每行格式为 <code>链接 [提取码] [标题]</code> 或 <code>链接|提取码|标题</code>（# 开头为注释）。支持批量：多个链接一行一个，自动逐个提取整理。</p>
<div class="row"><div><label>提取码（单行时使用）</label><input id="r-pw" placeholder="无则留空"></div>
<div><label>课程标题（单行时使用）</label><input id="r-ti" placeholder="默认自动命名"></div></div>
<button class="pri" onclick="startRun()">开始整理</button>
<div id="r-log" class="dim" style="margin-top:10px"></div>
<div id="r-batch"></div></div></section>
</main>
<script>
const $=s=>document.querySelector(s);const $$=s=>document.querySelectorAll(s);
let COURSE=null,DATA=null;
function h(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")}
const BSQ=String.fromCharCode(92);
function js(s){return (s||"").split(BSQ).join(BSQ+BSQ).split("'").join(BSQ+"'").split('"').join("&quot;")}
function show(t){["courses","course","groups","groupdetail","run"].forEach(x=>{$("#nav-"+({course:"courses",groupdetail:"groups"}[x]||x))?.classList.toggle("on",(x==="courses"&&(t==="courses"||t==="course"))||(x==="groups"&&(t==="groups"||t==="groupdetail")));$("#view-"+x).classList.toggle("hide",x!==t)});if(t==="courses")loadCourses();if(t==="groups")loadGroups()}
async function api(u,opt){const r=await fetch(u,opt);return r.json()}

async function loadGroups(){
  const d=await api("/api/groups");
  if(!d.groups||!d.groups.length){$("#group-list").innerHTML='<h3 style="margin-top:0">课程组</h3><p class="dim">尚无课程组。新课程整理完成后会自动按内容归组；也可运行 <code>python tools/merge_courses.py --force</code> 全量重建。</p>';return}
  $("#group-list").innerHTML=`<h3 style="margin-top:0">课程组（${d.groups.length}）</h3><p class="dim">按课程内容聚类聚合的相关课程（新课程整理完成后自动归组并更新），每组一份跨课程整合复习（体系全景图 / 学习主线 / 易混淆对照 / 学习顺序）。</p><table><tr><th>课程组</th><th>成员</th><th></th></tr>`+
   d.groups.map(g=>`<tr><td>${h(g.name)}</td><td class="dim">${g.count} 门</td><td><button class="pri" onclick="openGroup('${js(g.name)}')">打开整合复习</button></td></tr>`).join("")+`</table>`;
}
async function openGroup(name){
  const d=await api("/api/group?name="+encodeURIComponent(name));
  if(d.error){$("#group-body").innerHTML=`<div class="card"><span class="bad">${h(d.error)}</span></div>`;show("groupdetail");return}
  $("#group-body").innerHTML=`
  <button class="mini" onclick="show('groups')">← 返回课程组</button>
  <div class="card"><h2 style="margin:0 0 6px">${h(d.name)} · 整合复习</h2>
  <span class="dim">成员课程 ${d.courses.length} 门：</span><div>${d.courses.map(c=>`<button class="mini" style="margin:3px 4px 0 0" onclick="openCourse('${js(c.id)}')">${h(c.title)}</button>`).join("")}</div></div>
  <div class="card"><pre>${h(d.doc)}</pre></div>`;
  show("groupdetail");window.scrollTo(0,0);
}

async function loadCourses(){
  const d=await api("/api/courses");
  $("#course-list").innerHTML=`<h3 style="margin-top:0">我的课程（${d.courses.length}）</h3><table><tr><th>课程</th><th>质检</th><th>章节/习题</th><th></th></tr>`+
   d.courses.map(c=>`<tr><td>${h(c.title)}</td>
   <td class="${c.passed?'ok':'bad'}">${c.passed?'通过':'未过'} ${c.score}</td>
   <td class="dim">${c.sections} 章 / ${c.exercises} 题</td>
   <td><button class="pri" onclick="openCourse('${js(c.id)}')">进入</button></td></tr>`).join("")+
   `</table>`;
}
async function openCourse(id){
  DATA=await api("/api/course?id="+encodeURIComponent(id));COURSE=id;
  const s=DATA.session,secs=DATA.sections||[];
  $("#course-body").innerHTML=`
  <button class="mini" onclick="show('courses')">← 返回列表</button>
  <div class="card"><h2 style="margin:0 0 6px">${h(s.title)}</h2>
  <span class="${s.passed?'ok':'bad'}">质检${s.passed?'通过':'未过'} ${s.qa_score} 分</span>
  <span class="dim"> · ${s.sections} 章 · ${s.exercises} 题</span></div>
  <div class="card"><h3>📝 习题练习（逐章作答，交卷自动判分+生成解析）</h3><div id="quiz"></div>
  <div id="quiz-nav"></div><div id="quiz-sum" class="dim"></div><div id="quiz-review"></div></div>
  <div class="card"><h3>🔍 质检报告</h3><pre>${h(DATA.report)}</pre></div>
  <div class="card"><h3>🗒️ 原始笔记（含时间戳回看定位）</h3><pre>${h(DATA.notes)}</pre></div>
  <div class="card"><h3>📖 复习笔记（练习完成后用来巩固：概念+示例+易错点+相关拓展）</h3><pre id="review-pre">${h(DATA.review||"（本课程暂无复习笔记，可用 tools/regen_review.py 生成）")}</pre></div>`;
  show("course");renderQuiz(0);
}
let QIDX=0;
function renderQuiz(si){
  QIDX=si;const sec=(DATA.sections||[])[si];
  if(!sec){$("#quiz").innerHTML="（无此章）";$("#quiz-nav").innerHTML="";return}
  $("#quiz").innerHTML=`<h4 style="margin-top:0">${h(sec.title)}</h4>`+sec.exercises.map((e,i)=>{
    const t={single_choice:"单选",multi_choice:"多选",true_false:"判断",fill_blank:"填空",short_answer:"简答",coding_practice:"编程实战"}[e.type]||e.type;
    let body="";
    if(e.options&&e.options.length)body=e.options.map((o,j)=>`<label class="kp"><input type="${e.type==='multi_choice'?'checkbox':'radio'}" name="q${i}" value="${"ABCDEFGH"[j]}"> ${"ABCDEFGH"[j]}. ${h(o)}</label>`).join("");
    else if(e.type==="true_false")body=`<label class="kp"><input type="radio" name="q${i}" value="对"> 对</label><label class="kp"><input type="radio" name="q${i}" value="错"> 错</label>`;
    else if(e.type==="fill_blank")body=`<input id="a${i}" placeholder="填入答案">`;
    else if(e.type==="coding_practice")body=`<div class="editor"><pre class="gutter" id="g${i}">1</pre><textarea id="a${i}" class="code" spellcheck="false" placeholder="在此直接编写代码…">${h(e.starter_code||"")}</textarea></div>`+(e.hints&&e.hints.length?`<div class="dim">提示：${h(e.hints.join("；"))}</div>`:"")+`<button class="mini" onclick="runTests(${si},${i})">▶ 在线测试</button><div id="t${i}"></div>`+(e.answer?`<details><summary class="dim">查看完整参考实现</summary><pre>${h(e.answer)}</pre></details>`:"");
    else body=`<textarea id="a${i}" placeholder="写下你的回答，AI 对照参考答案点评"></textarea>`+(e.answer?`<details><summary class="dim">查看参考答案</summary><pre>${h(e.answer)}</pre></details>`:"");
    return `<div class="ex"><b>${i+1}.</b> ${h(e.question)}<span class="tag">${t}·${e.difficulty||""}</span>${body}<div id="r${i}"></div></div>`;
  }).join("");
  $("#quiz-nav").innerHTML=`<button class="pri" onclick="submitQuiz(${si})">交卷判分</button>
   ${si>0?`<button class="mini" onclick="gotoQuiz(${si},${si-1})">◀ 上一章</button>`:""}
   ${si<DATA.sections.length-1?`<button class="mini" onclick="gotoQuiz(${si},${si+1})">下一章 ▶</button>`:""}
   <span class="dim">第 ${si+1}/${DATA.sections.length} 章 · 切换时自动暂存本章作答</span>`;
  $("#quiz-sum").textContent="";
  DATA.sections[si].exercises.forEach((e,i)=>{if(e.type==="coding_practice")setupEditor(i)});
}
function setupEditor(i){
  const t=document.getElementById("a"+i),g=document.getElementById("g"+i);
  if(!t||!g)return;
  const upd=()=>{const n=t.value.split("\\n").length;g.textContent=Array.from({length:n},(_,k)=>k+1).join("\\n")};
  t.addEventListener("input",upd);
  t.addEventListener("scroll",()=>{g.scrollTop=t.scrollTop});
  upd();
}
async function gotoQuiz(cur,n){
  if(n<0||!DATA.sections[n])return;
  try{ // 静默暂存本章作答，切换不丢
    const rows=collect(cur);const sec={};
    rows.forEach((a,i)=>{sec[i]={user:a}});
    await api("/api/progress",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({course:COURSE,data:{sections:{[String(cur)]:sec}}})});
  }catch(e){}
  renderQuiz(n);
}
function collect(si){
  return (DATA.sections[si].exercises||[]).map((e,i)=>{
    // 选择类（单选/多选/判断）统一读 checked 项，判断题的 radio 也走这里
    const checked=[...document.querySelectorAll(`input[name=q${i}]:checked`)].map(x=>x.value).join("");
    if(checked)return checked;
    const el=document.getElementById("a"+i);return el?el.value:"";
  });
}
async function submitQuiz(si,move){
  const answers=collect(si);
  const d=await api("/api/grade",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({course:COURSE,section:si,answers})});
  if(d.error){alert(d.error);return}
  d.results.forEach(r=>{
    const el=document.getElementById("r"+r.idx);if(!el)return;
    if(r.score===null){el.innerHTML=`<div class="res mid">⚠️ ${h(r.note)}</div>`;return}
    const cls=r.correct?"ok":(r.score>=60?"mid":"bad");
    let extra=r.feedback?`<div class="dim">AI 点评：${h(r.feedback)}</div>`:"";
    if(!r.correct&&r.reference)extra+=`<details><summary class="dim">完整参考答案</summary><pre>${h(String(r.reference))}</pre></details>`;
    el.innerHTML=`<div class="res ${cls}">${r.correct?"✅":"❌"} ${r.score} 分 ${r.verdict||""} ${r.note?("· "+h(r.note)):""}</div>${extra}`;
  });
  $("#quiz-sum").textContent=`本章判分：${d.summary.correct}/${d.summary.graded} 题正确，平均 ${d.summary.avg} 分`+(d.summary.llm_error?`（主观题异常：${d.summary.llm_error}）`:"");
  const rv=document.getElementById("quiz-review");
  if(rv){
    if(d.review&&d.review.overall){
      let rhtml=`<div class="card" style="margin-top:12px"><h3 style="margin-top:0">📝 本次测验解析与强化知识</h3>
      <div class="kp"><b>整体评价：</b>${h(d.review.overall)}</div>`;
      if(d.review.items&&d.review.items.length)rhtml+=`<details open><summary>逐题解析</summary>`+d.review.items.map(it=>`<div class="kp"><b>第 ${it.idx+1} 题</b>：${h(it.analysis)}</div>`).join("")+`</details>`;
      if(d.review.weak_points&&d.review.weak_points.length)rhtml+=`<details open><summary>📌 薄弱点强化知识</summary>`+d.review.weak_points.map(w=>`<div class="kp"><b>${h(w.topic)}</b>：${h(w.content)}</div>`).join("")+`</details>`;
      if(d.review.suggestions&&d.review.suggestions.length)rhtml+=`<details><summary>🧭 复习建议</summary>`+d.review.suggestions.map(s=>`<div class="kp">· ${h(s)}</div>`).join("")+`</details>`;
      if(d.review.saved_to)rhtml+=`<div class="dim" style="margin-top:8px">已追加保存到课程目录：${h(d.review.saved_to)}</div>`;
      rv.innerHTML=rhtml+`</div>`;
    } else if(d.review&&d.review.error){
      rv.innerHTML=`<div class="res mid">解析生成失败：${h(d.review.error)}（判分结果不受影响）</div>`;
    } else { rv.innerHTML=""; }
  }
  if(move&&DATA.sections[si+1])renderQuiz(si+1);
}
async function runTests(si,i,regen){
  const el=document.getElementById("t"+i);if(!el)return;
  const e=DATA.sections[si].exercises[i];
  const codeEl=document.getElementById("a"+i);
  const code=codeEl?codeEl.value:"";
  el.innerHTML='<span class="dim">⏳ 运行测试中…</span>';
  const d=await api("/api/run_tests",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({course:COURSE,section:si,idx:i,code,regen:!!regen})});
  if(d.error){el.innerHTML=`<div class="res bad">❌ ${h(d.error)}</div>`;return}
  if(!d.runnable){el.innerHTML=`<div class="res mid">ℹ️ ${h(d.reason)}</div>`;return}
  let out=`<div class="res ${d.passed===d.total?'ok':'bad'}">在线测试：通过 ${d.passed}/${d.total}</div>`+
    d.results.map(r=>`<div class="${r.passed?'ok':'bad'}">${r.passed?"✅":"❌"} 断言${r.idx+1}${r.output?` <span class="dim">${h(r.output).slice(0,180)}</span>`:""}</div>`).join("");
  if(d.mismatch){
    out+=`<div class="res mid">🔀 ${h(d.mismatch.hint)}</div>`;
    out+=`<button class="mini" onclick="runTests(${si},${i},true)">🔄 按我的代码重新生成测试</button>`;
  }
  el.innerHTML=out;
}

async function startRun(){
  const inp=$("#r-in").value;
  if(!inp.trim()){alert("请填写至少一个课程链接或文件路径");return;}
  const d=await api("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({input:inp,password:$("#r-pw").value,title:$("#r-ti").value})});
  if(d.error){alert(d.error);return;}
  $("#r-batch").innerHTML="";
  $("#r-log").textContent="已提交 "+(d.count||1)+" 个任务，后台自动提取整理中（腾讯回放先提取逐字稿，本地大模型整理每门约数分钟）…";
}
const _BST={pending:"⏳",running:"⚙️",done:"✅",qa_failed:"△",failed:"❌"};
setInterval(()=>{api("/api/run/status").then(s=>{
  const el=$("#r-log"),bb=$("#r-batch");if(!el)return;
  if(s.log)el.textContent=s.log;
  if(bb&&s.batch&&s.batch.length){
    bb.innerHTML='<div class="dim" style="margin:8px 0">进度 '+(s.done||0)+'/'+(s.total||s.batch.length)+'</div>'+
      s.batch.map(b=>'<div class="row" style="padding:2px 0">'+(_BST[b.status]||"?")+" "+h(b.title)+
      (b.status==="done"?"（"+b.score+" 分）":"")+(b.error?' <span class="bad">'+h(b.error)+"</span>":"")+"</div>").join("");
  }
  if(!s.running&&s.result){const r=s.result;
    if(r.batch){el.innerHTML=(r.passed===r.total?`<span class="ok">✅ 批量完成：${r.passed}/${r.total} 门通过质检</span>`:`<span class="bad">△ 批量结束：${r.passed||0}/${r.total} 门通过</span>`)+' <a href="#" onclick="loadCourses();show(&quot;courses&quot;);return false">刷新课程列表</a>';}
    else el.innerHTML=r.ok?`<span class="ok">✅ 完成：${h(r.title)}（${r.score} 分）</span> <a href="#" onclick="loadCourses();show('courses');return false">刷新课程列表</a>`:`<span class="bad">❌ ${h(r.error||"失败")}</span>`;}
}).catch(()=>{})},4000);
document.addEventListener("keydown",(ev)=>{
  if(ev.key!=="Tab"||!ev.target||!ev.target.classList||!ev.target.classList.contains("code"))return;
  ev.preventDefault();
  const t=ev.target,s=t.selectionStart,en=t.selectionEnd;
  t.value=t.value.slice(0,s)+"    "+t.value.slice(en);
  t.selectionStart=t.selectionEnd=s+4;
});
loadCourses();
</script></body></html>
"""


# ------------------------------------------------------------------ 入口
def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(description="CourseAgent 图形界面")
    ap.add_argument("--port", type=int, default=_PORT)
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv or [])

    cfg = load_config(args.config)

    # pythonw（无控制台）下 stdout/stderr 是 None，任何 print 都会崩。
    # 重定向：stdout 丢弃，stderr 落到日志文件便于排查启动问题。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        errlog = Path(cfg.data_dir) / "webui_stderr.log"
        errlog.parent.mkdir(parents=True, exist_ok=True)
        sys.stderr = open(errlog, "a", encoding="utf-8", errors="replace")

    setup_logging()
    Handler.cfg = cfg
    Handler.llm = _build_llm(cfg)

    server = None
    used_port = args.port
    for port in (args.port, args.port + 1, args.port + 2):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            used_port = port
            break
        except OSError:
            continue  # 端口被占（多半已有实例）自动顺延
    if server is None:
        print(f"[CourseAgentUI] 端口 {args.port}~{args.port + 2} 均无法绑定")
        return 1

    url = f"http://127.0.0.1:{used_port}"
    print(f"CourseAgent 图形界面已启动: {url} （Ctrl+C 停止）")
    # 启动补归组：已过质检但未进任何课程组的历史课程，后台自动归组
    threading.Thread(target=_autogroup_backfill, args=(cfg,), daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
