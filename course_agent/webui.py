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
import re
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
                rows = []
                for d in _course_dirs(self.cfg):
                    try:
                        s = json.loads((d / "session.json").read_text(encoding="utf-8"))
                        rows.append({"id": _course_id(self.cfg, d),
                                     "title": s.get("title", d.name),
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
                return self._json({"id": _course_id(self.cfg, d), "session": session,
                                   "sections": sections, "notes": notes,
                                   "report": report,
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
textarea{min-height:90px;resize:vertical}
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
  <div class="card"><h3>🗒️ 知识点笔记</h3><pre>${h(DATA.notes)}</pre></div>
  <div class="card"><h3>🔍 质检报告</h3><pre>${h(DATA.report)}</pre></div>`;
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
    else if(e.type==="coding_practice")body=(e.starter_code?`<pre>${h(e.starter_code)}</pre>`:"")+`<textarea id="a${i}" placeholder="粘贴你的实现代码，AI 将对照验收要点评审"></textarea>`+(e.hints&&e.hints.length?`<div class="dim">提示：${h(e.hints.join("；"))}</div>`:"");
    else body=`<textarea id="a${i}" placeholder="写下你的回答，AI 对照参考答案点评"></textarea>`;
    return `<div class="ex"><b>${i+1}.</b> ${h(e.question)}<span class="tag">${t}·${e.difficulty||""}</span>${body}<div id="r${i}"></div></div>`;
  }).join("");
  $("#quiz-nav").innerHTML=`<button class="pri" onclick="submitQuiz(${si})">交卷判分</button>
   ${si>0?`<button class="mini" onclick="renderQuiz(${si-1})">上一章</button>`:""}
   ${si<DATA.sections.length-1?`<button class="mini" onclick="nextQuiz(${si})">下一章（先交卷）</button>`:""}`;
  $("#quiz-sum").textContent="";
}
function nextQuiz(si){submitQuiz(si,true)}
function collect(si){
  return (DATA.sections[si].exercises||[]).map((e,i)=>{
    if(e.options&&e.options.length)return[...document.querySelectorAll(`input[name=q${i}]:checked`)].map(x=>x.value).join("");
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
    if(!r.correct&&r.reference)extra+=`<div class="dim">参考：${h(String(r.reference)).slice(0,220)}</div>`;
    el.innerHTML=`<div class="res ${cls}">${r.correct?"✅":"❌"} ${r.score} 分 ${r.verdict||""} ${r.note?("· "+h(r.note)):""}</div>${extra}`;
  });
  $("#quiz-sum").textContent=`本章判分：${d.summary.correct}/${d.summary.graded} 题正确，平均 ${d.summary.avg} 分`+(d.summary.llm_error?`（主观题异常：${d.summary.llm_error}）`:"");
  if(move&&DATA.sections[si+1])renderQuiz(si+1);
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
loadCourses();
</script></body></html>
"""


# ------------------------------------------------------------------ 入口
def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="CourseAgent 图形界面")
    ap.add_argument("--port", type=int, default=_PORT)
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv or [])

    setup_logging()
    cfg = load_config(args.config)
    Handler.cfg = cfg
    Handler.llm = _build_llm(cfg)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
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
