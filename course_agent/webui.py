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
        u = user.strip().upper()[:1]
        ok = u == ref.strip().upper()[:1]
        return ok, f"正确答案 {ref.strip().upper()[:1]}"
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
                   "请只输出评分 JSON。", temperature=0.2, max_tokens=512)
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
_RUN_STATE: dict[str, Any] = {"running": False, "log": "", "result": None}
_RUN_LOCK = threading.Lock()


def _run_pipeline_async(input_src: str, password: str, title: str, cfg: AgentConfig) -> None:
    import logging

    from .ingest.loader import load_source
    from .memory import Memory
    from .outputs import write_all
    from .pipeline.orchestrator import Orchestrator

    log = logging.getLogger("webui.run")

    def run():
        with _RUN_LOCK:
            _RUN_STATE.update(running=True, log="加载素材…", result=None)
        try:
            mem = Memory(Path(cfg.data_dir) / "memory.db")
            try:
                llm = _build_llm(cfg)
                material = load_source(input_src, password=password, workdir=cfg.data_dir)
                course_title = title or material.title
                with _RUN_LOCK:
                    _RUN_STATE["log"] = f"整理中：{course_title}（本地大模型处理，约数分钟）"
                orch = Orchestrator(llm, cfg, memory=mem)
                result = orch.run(material, title=course_title)
                outdir = Path(cfg.output_dir) / "webui" / result.session_id
                result.output_dir = str(outdir)
                write_all(result, outdir)
                with _RUN_LOCK:
                    _RUN_STATE["result"] = {
                        "ok": result.passed, "title": course_title,
                        "score": result.qa.score if result.qa else 0,
                        "dir": str(outdir)}
                    _RUN_STATE["log"] = "完成"
            finally:
                mem.close()
        except Exception as e:  # 前端可见的错误
            log.error("运行失败: %s", e)
            with _RUN_LOCK:
                _RUN_STATE["result"] = {"ok": False, "error": truncate(str(e), 300)}
                _RUN_STATE["log"] = f"失败：{truncate(str(e), 160)}"
        finally:
            with _RUN_LOCK:
                _RUN_STATE["running"] = False

    threading.Thread(target=run, daemon=True).start()


def _build_llm(cfg: AgentConfig):
    from .llm import MockLLM, OpenAICompatClient, Router
    local = OpenAICompatClient(
        base_url=cfg.local.base_url, model=cfg.local.model,
        api_key=cfg.local.api_key, timeout=cfg.local.timeout,
        max_retries=cfg.local.max_retries)
    clients = [local]
    if cfg.api.enabled and cfg.api_key_effective:
        clients.append(OpenAICompatClient(
            base_url=cfg.api.base_url, model=cfg.api.model,
            api_key=cfg.api_key_effective, timeout=cfg.api.timeout,
            max_retries=cfg.api.max_retries))
    return Router(clients)


# ------------------------------------------------------------------ 在线测试
def run_code_tests(code: str, tests: list[str], language: str,
                   timeout: int = 10) -> dict:
    """编程题在线测试：把用户代码与每条断言拼接后真实执行。

    支持 python（本机解释器）与 javascript（需本机 node）；
    每条断言独立运行，返回逐条通过/失败与输出。
    """
    import shutil
    import subprocess
    import tempfile

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
        if language == "javascript":
            # node 无内置 assert，注入与浏览器约定一致的断言垫片
            script = ("function assert(cond, msg) { if (!cond) "
                      "throw new Error('AssertionError' + (msg ? ': ' + msg : '')); }\n"
                      + script)
        fd, tmp = tempfile.mkstemp(suffix=".py" if language == "python" else ".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(script)
            try:
                proc = subprocess.run([*runner, tmp], capture_output=True,
                                      text=True, timeout=timeout,
                                      encoding="utf-8", errors="replace")
                ok = proc.returncode == 0
                out = (proc.stderr or proc.stdout or "").strip()
                results.append({"idx": i, "test": test, "passed": ok,
                                "output": truncate(out, 400)})
            except subprocess.TimeoutExpired:
                results.append({"idx": i, "test": test, "passed": False,
                                "output": f"超时（>{timeout}s），检查是否存在死循环"})
        finally:
            Path(tmp).unlink(missing_ok=True)
    passed = sum(1 for r in results if r["passed"])
    return {"runnable": True, "results": results,
            "passed": passed, "total": len(results)}


def _backfill_tests(llm, exercise: dict) -> dict:
    """为没有 tests 的存量编程题现场生成在线测试断言。"""
    from .utils import extract_json

    prompt = (
        "根据编程题与其参考实现，生成在线测试断言。只输出 JSON："
        '{"language": "python|javascript|other", '
        '"tests": ["每条一行可独立执行的断言，python 用 assert，javascript 用 assert(条件)", ...]}'
        "要求：2~4 条，覆盖正常与边界情况；断言基于题干规定的函数名与参考实现语义；"
        "若题目是框架/页面类无法断言验证，language 为 other 且 tests 为空数组。\n\n"
        f"【题目】{exercise.get('question', '')}\n"
        f"【参考实现】\n{exercise.get('answer', '')}\n"
        f"【验收要点】{json.dumps(exercise.get('check_points') or [], ensure_ascii=False)}\n"
    )
    raw = llm.chat(prompt, "请只输出 JSON。", temperature=0.2, max_tokens=512)
    data = extract_json(raw)
    lang = data.get("language")
    tests = [str(t).strip() for t in data.get("tests") or [] if str(t).strip()]
    return {"language": lang if lang in ("python", "javascript", "other") else "other",
            "tests": tests[:4]}


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
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


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
        n = int(self.headers.get("Content-Length") or 0)
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
                rows = []
                for d in _course_dirs(self.cfg):
                    try:
                        s = json.loads((d / "session.json").read_text(encoding="utf-8"))
                        cid = _course_id(self.cfg, d)
                        rows.append({"id": cid,
                                     "title": overrides.get(cid) or s.get("title", d.name),
                                     "passed": s.get("passed", False),
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
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # 防单请求异常拖垮服务
            return self._json({"error": str(e)}, 500)

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
                if not tests or not lang:
                    # 存量编程题缺测试断言：现场生成并回写缓存
                    try:
                        back = _backfill_tests(self.llm, ex or {
                            "question": str(body.get("question") or ""),
                            "answer": str(body.get("answer") or "")})
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
                return self._json(run_code_tests(code, tests, lang))
            if u.path == "/api/progress":
                cid = body.get("course", "")
                if not cid:
                    return self._json({"error": "course required"}, 400)
                data = _load_progress(self.cfg, cid)
                data.update(body.get("data") or {})
                _save_progress(self.cfg, cid, data)
                return self._json({"ok": True})
            if u.path == "/api/run":
                src = (body.get("input") or "").strip()
                if not src:
                    return self._json({"error": "input required"}, 400)
                if _RUN_STATE["running"]:
                    return self._json({"error": "已有任务在运行，请稍候"}, 409)
                _run_pipeline_async(src, body.get("password", ""),
                                    body.get("title", ""), self.cfg)
                return self._json({"ok": True})
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
        # 记入进度
        prog = _load_progress(self.cfg, cid)
        sec_prog = prog.setdefault("sections", {}).setdefault(str(si), {})
        for r in results:
            sec_prog[str(r["idx"])] = {"user": answers[r["idx"]] if r["idx"] < len(answers) else "",
                                       "score": r["score"], "correct": r["correct"]}
        prog.setdefault("sections", {})[str(si)] = sec_prog
        _save_progress(self.cfg, cid, prog)
        graded = [r for r in results if r["score"] is not None]
        return {"results": results,
                "summary": {"graded": len(graded),
                            "correct": sum(1 for r in graded if r["correct"]),
                            "avg": round(sum(r["score"] for r in graded) / len(graded)) if graded else 0,
                            "llm_error": llm_err}}


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
<button id="nav-run" onclick="show('run')">新任务</button>
</nav></header>
<main>
<section id="view-courses"><div class="card" id="course-list"><span class="dim">加载中…</span></div></section>

<section id="view-course" class="hide"><div id="course-body"></div></section>

<section id="view-run" class="hide"><div class="card"><h3>➕ 新课程整理任务</h3>
<div class="row"><div><label>素材（文件路径 / 回放链接 / 转写json）</label>
<input id="r-in" placeholder="例：D:/录屏/xx.mp4 或 https://meeting.tencent.com/crm/xxx"></div>
<div><label>提取码（可选）</label><input id="r-pw" placeholder="无则留空"></div></div>
<label>课程标题（可选）</label><input id="r-ti" placeholder="默认取文件名">
<p class="dim">腾讯会议回放无法自动提取时，先用 F12 抓 minutes/detail 走 tools/convert_tencent_minutes.py 生成转写 json 再提交。</p>
<button class="pri" onclick="startRun()">开始整理</button>
<div id="r-log" class="dim" style="margin-top:10px"></div></div></section>
</main>
<script>
const $=s=>document.querySelector(s);const $$=s=>document.querySelectorAll(s);
let COURSE=null,DATA=null;
function h(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")}
function show(t){["courses","course","run"].forEach(x=>{$("#nav-"+(x==="course"?"courses":x))?.classList.toggle("on",x!=="run"&&t!=="run");$("#view-"+x).classList.toggle("hide",x!==t)});if(t==="courses")loadCourses()}
async function api(u,opt){const r=await fetch(u,opt);return r.json()}

async function loadCourses(){
  const d=await api("/api/courses");
  $("#course-list").innerHTML=`<h3 style="margin-top:0">我的课程（${d.courses.length}）</h3><table><tr><th>课程</th><th>质检</th><th>章节/习题</th><th></th></tr>`+
   d.courses.map(c=>`<tr><td>${h(c.title)}</td>
   <td class="${c.passed?'ok':'bad'}">${c.passed?'通过':'未过'} ${c.score}</td>
   <td class="dim">${c.sections} 章 / ${c.exercises} 题</td>
   <td><button class="pri" onclick="openCourse('${c.id}')">进入</button></td></tr>`).join("")+
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
  <div class="card"><h3>📝 习题练习（逐章作答，交卷自动判分）</h3><div id="quiz"></div>
  <div id="quiz-nav"></div><div id="quiz-sum" class="dim"></div></div>
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
  if(move&&DATA.sections[si+1])renderQuiz(si+1);
}
async function runTests(si,i){
  const el=document.getElementById("t"+i);if(!el)return;
  const e=DATA.sections[si].exercises[i];
  const codeEl=document.getElementById("a"+i);
  const code=codeEl?codeEl.value:"";
  el.innerHTML='<span class="dim">⏳ 运行测试中…</span>';
  const d=await api("/api/run_tests",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({code,tests:e.tests,language:e.language||"python"})});
  if(d.error){el.innerHTML=`<div class="res bad">❌ ${h(d.error)}</div>`;return}
  if(!d.runnable){el.innerHTML=`<div class="res mid">ℹ️ ${h(d.reason)}</div>`;return}
  el.innerHTML=`<div class="res ${d.passed===d.total?'ok':'bad'}">在线测试：通过 ${d.passed}/${d.total}</div>`+
    d.results.map(r=>`<div class="${r.passed?'ok':'bad'}">${r.passed?"✅":"❌"} 断言${r.idx+1}${r.output?` <span class="dim">${h(r.output).slice(0,180)}</span>`:""}</div>`).join("");
}

async function startRun(){
  const d=await api("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({input:$("#r-in").value,password:$("#r-pw").value,title:$("#r-ti").value})});
  if(d.error)alert(d.error);else $("#r-log").textContent="已提交，后台整理中（本地大模型，约数分钟）…";
}
setInterval(()=>{api("/api/run/status").then(s=>{
  const el=$("#r-log");if(!el)return;
  if(s.log)el.textContent=s.log;
  if(!s.running&&s.result){const r=s.result;el.innerHTML=r.ok?`<span class="ok">✅ 完成：${h(r.title)}（${r.score} 分）</span> <a href="#" onclick="loadCourses();show('courses');return false">刷新课程列表</a>`:`<span class="bad">❌ ${h(r.error||"失败")}</span>`}
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
