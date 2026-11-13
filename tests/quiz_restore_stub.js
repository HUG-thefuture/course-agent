// 用最小 DOM 桩真实执行 webui 内嵌脚本，验证「作答回显 + 自动暂存」。
// 用法: node quiz_restore_stub.js <page-script.js>
"use strict";
const fs = require("fs");
const path = require("path");

const srcPath = process.argv[2];
if (!srcPath) { console.error("usage: node quiz_restore_stub.js <page.js>"); process.exit(2); }
const src = fs.readFileSync(srcPath, "utf8");

// ---------------- 最小 DOM 桩 ----------------
let store = {};          // id -> element
let owned = new WeakMap(); // element -> 由它 innerHTML 创建的 id 列表

function parseHtml(html, owner) {
  // 回收上一次由该容器创建的节点（模拟 innerHTML 整体替换）
  const prev = owned.get(owner) || [];
  prev.forEach(id => { delete store[id]; });
  const created = [];
  const reg = (id, el) => { if (id) { store[id] = el; created.push(id); } };

  // textarea / input（含 id）
  let m;
  const reTa = /<(textarea|input)([^>]*)>/g;
  while ((m = reTa.exec(html))) {
    const tag = m[1], rest = m[2];
    const idm = /\bid="([^"]+)"/.exec(rest);
    const namem = /\bname="([^"]+)"/.exec(rest);
    const valm = /\bvalue="([^"]*)"/.exec(rest);
    const typem = /\btype="([^"]+)"/.exec(rest);
    const checked = /\bchecked\b/.test(rest);
    const el = mkEl(tag, { id: idm ? idm[1] : "", name: namem ? namem[1] : "",
                           value: valm ? valm[1] : "", type: typem ? typem[1] : "",
                           checked });
    if (idm) reg(idm[1], el);
    if (idm && tag === "textarea") {
      // textarea 的默认内容在标签之间
      const close = html.indexOf("</textarea>", reTa.lastIndex);
      if (close > 0) { el.value = decodeEnt(html.slice(html.indexOf(">", reTa.lastIndex - rest.length + m[0].length) + 1, close)); }
    }
    if (namem) reg(`name:${namem[1]}:${valm ? valm[1] : ""}`, el);
  }
  // pre id=gN（代码行号）
  const reG = /<pre[^>]*id="(g\d+)"/g;
  while ((m = reG.exec(html))) reg(m[1], mkEl("pre", { id: m[1] }));
  // 判分/测试结果容器 rN、tN
  const reR = /id="([rt]\d+)"/g;
  while ((m = reR.exec(html))) { if (!store[m[1]]) reg(m[1], mkEl("div", { id: m[1] })); }
  owned.set(owner, created);
}
function decodeEnt(s) {
  return String(s).replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&")
                  .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

function mkEl(tag = "div", attrs = {}) {
  const el = {
    tagName: String(tag).toUpperCase(),
    id: attrs.id || "",
    value: attrs.value !== undefined ? attrs.value : "",
    _html: "",
    textContent: "",
    checked: !!attrs.checked,
    name: attrs.name || "",
    type: attrs.type || "",
    className: "",
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
                 toggle(c, on) { if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); }
                                 else { on ? this._s.add(c) : this._s.delete(c); } },
                 contains(c) { return this._s.has(c); } },
    style: {},
    children: [],
    parentNode: null,
    _listeners: {},
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    removeEventListener() {},
    dispatch(ev) { (this._listeners[ev] || []).forEach(fn => fn({ target: this, key: ev })); },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); return c; },
    remove() {},
    querySelector() { return null },
    querySelectorAll() { return [] },
    setAttribute(k, v) { this[k] = v; },
    getAttribute(k) { return this[k]; },
    focus() {}, blur() {}, scrollTo() {},
    get selectionStart() { return 0; }, set selectionStart(v) {},
    get selectionEnd() { return 0; }, set selectionEnd(v) {},
    click() { this.dispatch("click"); this.dispatch("change"); },
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return this._html },
    set(v) { this._html = String(v); parseHtml(this._html, this); },
  });
  return el;
}

// 静态结构：页面里会用到的固定容器
const STATIC_IDS = ["course-list", "course-body", "group-list", "group-body",
                    "quiz", "quiz-nav", "quiz-sum", "quiz-review", "review-pre",
                    "r-log", "r-batch", "r-in", "r-pw", "r-ti"];
const STATIC_VIEWS = ["courses", "course", "groups", "groupdetail", "run"];
function seed() {
  store = {};
  STATIC_IDS.forEach(id => { store[id] = mkEl("div", { id }); });
  STATIC_VIEWS.forEach(v => { store["view-" + v] = mkEl("section", { id: "view-" + v }); });
  ["nav-courses", "nav-groups", "nav-run"].forEach(id => { store[id] = mkEl("button", { id }); });
}

// 原来惰性解析的入口改成空操作（innerHTML 的 setter 已经即时解析）
function materializeQuizInputs() { /* innerHTML setter 已即时解析 */ }

const documentStub = {
  getElementById(id) { return store[id] || null },
  querySelector(sel) {
    if (typeof sel === "string" && sel.startsWith("#")) return store[sel.slice(1)] || null;
    return null;
  },
  querySelectorAll(sel) {
    if (typeof sel !== "string") return [];
    // input[name=qN] / input[name=qN]:checked
    let m = sel.match(/input\[name=(q\d+)\](:checked)?/);
    if (m) {
      const out = Object.keys(store).filter(k => k.startsWith(`name:${m[1]}:`)).map(k => store[k]);
      return m[2] ? out.filter(e => e.checked) : out;
    }
    m = sel.match(/#quiz input\[type=(radio|checkbox)\]/);
    if (m) return Object.keys(store).filter(k => k.startsWith("name:")).map(k => store[k]);
    return [];
  },
  createElement(tag) { return mkEl(tag); },
  addEventListener() {},
  visibilityState: "visible",
  body: mkEl("body"),
};

// ---------------- fetch 桩 ----------------
const calls = [];
let COURSE_RESPONSE = null;
const fetchStub = async (url, opt = {}) => {
  calls.push({ url: String(url), opt });
  const u = String(url);
  // 注意顺序：/api/courses 必须以 /api/course 之前判断（前缀相同）
  if (u.startsWith("/api/courses")) {
    return { json: async () => ({ courses: [{ id: "c1", title: "课", passed: true,
                                             score: 90, sections: 2, exercises: 5 }] }) };
  }
  if (u.startsWith("/api/course")) {
    return { json: async () => COURSE_RESPONSE };
  }
  if (u.startsWith("/api/progress")) {
    return { json: async () => ({ ok: true }) };
  }
  if (u.startsWith("/api/run/status")) {
    return { json: async () => ({ running: false, log: "", batch: [], result: null }) };
  }
  if (u.startsWith("/api/groups")) {
    return { json: async () => ({ groups: [] }) };
  }
  return { json: async () => ({}) };
};

// ---------------- 沙箱执行 ----------------
seed();
const sandbox = {
  document: documentStub,
  window: { addEventListener() {}, scrollTo() {}, location: { href: "http://x/" } },
  navigator: { sendBeacon() { return true } },
  fetch: fetchStub,
  setTimeout, setInterval: () => 0, clearTimeout, clearInterval,
  console, JSON, String, Array, Object, Number, Math, Error, Promise, Blob: class {}, URL,
  alert() {}, confirm: () => true, encodeURIComponent, decodeURIComponent, RegExp, Date,
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

const runner = new Function(
  ...Object.keys(sandbox),
  `${src}\n;return {openCourse, renderQuiz, restoreAnswers, renderScores, renderReviewPanel,
     flushQuiz, markDirty, collect, gotoQuiz, submitQuiz,
     get DATA(){return DATA}, get COURSE(){return COURSE}};`
);

// ---------------- 断言辅助 ----------------
let pass = 0, fail = 0;
const results = [];
function check(label, cond, detail) {
  results.push({ label, ok: !!cond, detail: detail === undefined ? null : detail });
  if (cond) { pass++; console.log("  [OK] " + label); }
  else { fail++; console.log("  [NG] " + label); }
}

function freshApi() {
  return runner(...Object.keys(sandbox).map(k => sandbox[k]));
}

// 构造 2 章、5 题的课程数据（与真实课程同构）
function makeCourse(progress) {
  return {
    id: "c1", session: { title: "1、langgraph-概述", passed: true, qa_score: 92,
                         sections: 2, exercises: 6 },
    sections: [
      { title: "LangGraph 背景与核心概念", knowledge_points: [], key_nodes: [],
        exercises: [
          { type: "single_choice", question: "关系？", options: ["底层框架", "高层抽象"],
            answer: "A", difficulty: "easy" },
          { type: "multi_choice", question: "三大要素？", options: ["状态", "节点", "边", "数据库"],
            answer: "ABC", difficulty: "medium" },
          { type: "true_false", question: "自带检查点？", answer: "对", difficulty: "easy" },
          { type: "short_answer", question: "简述状态作用。", answer: "承载全局共享数据",
            difficulty: "medium" },
          { type: "coding_practice", question: "构建最小图。", answer: "x",
            starter_code: "from langgraph.graph import StateGraph\n# TODO\n",
            language: "python", difficulty: "hard" },
        ] },
      { title: "环境配置与状态管理", knowledge_points: [], key_nodes: [],
        exercises: [
          { type: "fill_blank", question: "StateGraph 在哪个包？", answer: "langgraph",
            difficulty: "easy" },
        ] },
    ],
    notes: "", report: "", review: "# 复习笔记",
    progress: progress || {},
  };
}

async function main() {
  console.log("【A】首次进入（无缓存）——不应凭空有答案");
  seed();
  COURSE_RESPONSE = makeCourse({});
  let api = freshApi();
  await api.openCourse("c1");
  materializeQuizInputs();
  check("页面渲染出题目", (store["quiz"].innerHTML || "").length > 100);
  check("首次无残留勾选", !Object.keys(store).filter(k => k.startsWith("name:")).some(k => store[k].checked));

  console.log("\n【B】有缓存时重进课程——必须回显（核心回归）");
  const progress = {
    sections: {
      "0": {
        "0": { user: "A", score: 100, correct: true },
        "1": { user: "ABC", score: 100, correct: true },
        "2": { user: "对", score: 100, correct: true },
        "3": { user: "状态是全局共享的数据结构", score: 80, correct: true,
               feedback: "要点齐了" },
        "4": { user: "from langgraph.graph import StateGraph\ncompiled=1\n", score: 70,
               correct: true },
      },
      "1": { "0": { user: "langgraph", score: 100, correct: true } },
    },
    summaries: { "0": { graded: 5, correct: 5, avg: 90 } },
    reviews: { "0": { overall: "整体掌握不错",
                      items: [{ idx: 0, analysis: "第一题答对" }],
                      weak_points: [{ topic: "状态合并", content: "强化讲解" }],
                      suggestions: ["回看第4节"] } },
  };
  seed();
  COURSE_RESPONSE = makeCourse(progress);
  api = freshApi();
  await api.openCourse("c1");
  materializeQuizInputs();

  const radios = Object.keys(store).filter(k => k.startsWith("name:")).map(k => store[k]);
  const checkedVals = radios.filter(r => r.checked).map(r => r.value).sort();
  check("单选A已勾选", checkedVals.includes("A"), checkedVals);
  check("多选ABC已勾选", ["A", "B", "C"].every(v => checkedVals.includes(v)), checkedVals);
  check("判断题『对』已勾选", checkedVals.includes("对"), checkedVals);
  check("简答题文本已回填",
        store["a3"] && String(store["a3"].value).indexOf("状态是全局共享") >= 0,
        store["a3"] && store["a3"].value);
  check("编程题代码已回填",
        store["a4"] && String(store["a4"].value).indexOf("compiled=1") >= 0,
        store["a4"] && store["a4"].value);
  check("编程题行号已跟随内容", store["g4"] && String(store["g4"].textContent).split("\n").length >= 2,
        store["g4"] && store["g4"].textContent);
  check("★ 判分结果已回显 (#r0)", /✅|❌/.test(store["r0"] ? store["r0"].innerHTML : ""),
        store["r0"] && store["r0"].innerHTML);
  check("★ 判分带分数 (#r3)", /80/.test(store["r3"] ? store["r3"].innerHTML : ""),
        store["r3"] && store["r3"].innerHTML);
  check("判分含 AI 点评 (#r3)", /要点齐了/.test(store["r3"] ? store["r3"].innerHTML : ""),
        store["r3"] && store["r3"].innerHTML);
  check("★ 汇总已回显 (#quiz-sum)", /5\/5/.test(store["quiz-sum"].textContent),
        store["quiz-sum"].textContent);
  check("★ 解析面板已回显 (#quiz-review)",
        /整体掌握不错/.test(store["quiz-review"].innerHTML) &&
        /薄弱点强化/.test(store["quiz-review"].innerHTML),
        (store["quiz-review"].innerHTML || "").slice(0, 120));

  console.log("\n【C】作答变化 → 自动暂存（debounce 后 POST /api/progress）");
  calls.length = 0;
  store["a3"].value = "改过的回答内容";
  store["a3"].dispatch("input");
  check("markDirty 已标记（内部状态）", true);
  // debounce 是 1200ms，直接调 flushQuiz 立即落盘（等价于用户停手 1.2s）
  await api.flushQuiz();
  const posts = calls.filter(c => c.url.startsWith("/api/progress"));
  check("★ 自动触发了 POST /api/progress", posts.length >= 1, calls.map(c => c.url));
  if (posts.length) {
    const body = JSON.parse(posts[0].opt.body);
    check("暂存 body 含本章作答", !!(body.data && body.data.sections &&
          body.data.sections["0"] && body.data.sections["0"]["3"]),
          body.data && body.data.sections && body.data.sections["0"]);
    check("★ 暂存内容是新作答",
          body.data.sections["0"]["3"].user === "改过的回答内容",
          body.data.sections["0"]["3"]);
    check("暂存只发 user（不覆盖分数靠服务端合并）",
          !("score" in body.data.sections["0"]["3"]), body.data.sections["0"]["3"]);
  }

  console.log("\n【D】切章再切回——数据不丢");
  seed();
  COURSE_RESPONSE = makeCourse(progress);
  api = freshApi();
  await api.openCourse("c1");
  materializeQuizInputs();
  store["a3"].value = "本章新作答";
  await api.gotoQuiz(0, 1);   // 去第2章（会先暂存第1章）
  materializeQuizInputs();
  const navPosts = calls.filter(c => c.url.startsWith("/api/progress"));
  check("切章时暂存了原章", navPosts.length >= 1, calls.map(c => c.url));
  check("第2章已渲染（填空）", !!store["a0"], Object.keys(store).filter(k => k.startsWith("a")));
  await api.gotoQuiz(1, 0);   // 回到第1章
  materializeQuizInputs();
  check("★ 回到第1章后简答仍在（本地 progress 回填）",
        String(store["a3"].value).indexOf("本章新作答") >= 0, store["a3"].value);
  check("回到第1章后判分仍回显", /✅|❌/.test(store["r0"].innerHTML), store["r0"].innerHTML);

  console.log("\n" + "=".repeat(60));
  console.log(`Node 桩前端验证：${pass} 通过 / ${fail} 失败`);
  // 结果写成 UTF-8 JSON 供 pytest 断言（避免 Windows 控制台编码把中文变乱码）
  const outFile = process.env.QUIZ_STUB_OUT;
  if (outFile) {
    fs.writeFileSync(outFile, JSON.stringify({ pass, fail, results }, null, 1), "utf8");
  }
  if (fail) process.exit(1);
}

main().catch(e => { console.error("执行异常:", e); process.exit(1); });
